"""
Camada de banco de dados do Sistema Dois Irmãos.

COMO FUNCIONA
-------------
Todo acesso ao banco feito pela tela passa por UMA thread dedicada
(classe Banco). A tela só "pede" uma operação com banco.enviar(...) e
recebe o resultado depois, num callback. Assim:
  * a tela NUNCA congela esperando o banco;
  * nunca há duas gravações ao mesmo tempo (sem "database is locked");
  * cada operação roda numa transação: ou grava tudo, ou nada.

As operações são funções simples no formato  funcao(conn, ...)  e
podem ser testadas sem a tela.

ESTRUTURA (versão 3)
--------------------
  prods            id, nome, nome_norm, preco, estoque
  clientes         id, nome, nome_norm, endereco, endereco_norm
  historico        id, data (DD/MM/AAAA HH:MM), data_iso (AAAA-MM-DD HH:MM),
                   cliente, total, forma, taxa, itens_json
  itens_venda      id, venda_id, produto_id, nome, qtd, preco_unit, total
  entradas_estoque id, nome_prod, produto_id, qtd, data, data_iso

 * nome_norm = nome sem acentos/maiúsculo, com índice (busca rápida).
 * data_iso  = data em formato ordenável, com índice (relatórios por
   período filtram direto no SQL).
 * itens_venda substitui a leitura do itens_json (que continua sendo
   gravado só por compatibilidade com versões antigas do programa).
"""
import hashlib
import json
import os
import queue
import re
import sqlite3
import threading
import time
import unicodedata
from datetime import datetime, timedelta

FORMATO_DATA = "%d/%m/%Y %H:%M"
FORMATO_ISO = "%Y-%m-%d %H:%M"
VERSAO_SCHEMA = 3


# =============================================================================
# UTILITÁRIOS
# =============================================================================
def normalizar_texto(valor):
    """'João  da Silva' -> 'JOAO DA SILVA' (sem acentos, espaços extras, maiúsculo)."""
    if valor is None:
        return ""
    texto = " ".join(str(valor).split())
    texto = unicodedata.normalize("NFKD", texto)
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return texto.upper()


def data_para_iso(data_str):
    """'25/12/2025 14:30' -> '2025-12-25 14:30'. Retorna '' se não der para ler."""
    if not data_str:
        return ""
    for fmt, tam in ((FORMATO_DATA, None), ("%d/%m/%Y", 10)):
        try:
            texto = data_str if tam is None else data_str[:tam]
            return datetime.strptime(texto.strip(), fmt).strftime(FORMATO_ISO)
        except (ValueError, TypeError):
            continue
    return ""


def conectar(caminho):
    conn = sqlite3.connect(caminho, timeout=15, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _colunas(conn, tabela):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({tabela})")}


def _carregar_json(itens_json):
    try:
        itens = json.loads(itens_json) if itens_json else []
        return [i for i in itens if isinstance(i, dict)] if isinstance(itens, list) else []
    except Exception:
        return []


def _num(valor, padrao=0.0):
    try:
        return float(valor)
    except (TypeError, ValueError):
        return padrao


# =============================================================================
# CRIAÇÃO / MIGRAÇÃO
# =============================================================================
def preparar_banco(conn, pasta_backup=None):
    """Cria as tabelas, migra bancos antigos para a versão atual e
    completa dados gravados por versões antigas do programa."""
    conn.execute("CREATE TABLE IF NOT EXISTS prods (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, preco REAL)")
    conn.execute("CREATE TABLE IF NOT EXISTS clientes (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, endereco TEXT)")
    conn.execute("""CREATE TABLE IF NOT EXISTS historico
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, cliente TEXT, total REAL,
                     forma TEXT, taxa REAL, itens_json TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS entradas_estoque
                    (id INTEGER PRIMARY KEY AUTOINCREMENT, nome_prod TEXT, qtd INTEGER, data TEXT)""")
    conn.commit()

    versao = conn.execute("PRAGMA user_version").fetchone()[0]
    if versao < VERSAO_SCHEMA:
        tem_dados = conn.execute("SELECT (SELECT COUNT(*) FROM historico) + (SELECT COUNT(*) FROM prods)").fetchone()[0]
        if tem_dados and pasta_backup:
            # Cópia de segurança ANTES de mexer na estrutura do banco
            os.makedirs(pasta_backup, exist_ok=True)
            destino = os.path.join(pasta_backup, f"loja_antes_v{VERSAO_SCHEMA}_{datetime.now():%Y-%m-%d_%H-%M-%S}.db")
            copiar_banco(conn, destino)

    novas_colunas = {
        "prods": [("estoque", "REAL DEFAULT 0"), ("nome_norm", "TEXT")],
        "clientes": [("nome_norm", "TEXT"), ("endereco_norm", "TEXT")],
        "historico": [("data_iso", "TEXT")],
        "entradas_estoque": [("data_iso", "TEXT"), ("produto_id", "INTEGER")],
    }
    with conn:
        for tabela, cols in novas_colunas.items():
            existentes = _colunas(conn, tabela)
            for nome, tipo in cols:
                if nome not in existentes:
                    conn.execute(f"ALTER TABLE {tabela} ADD COLUMN {nome} {tipo}")

        conn.execute("""CREATE TABLE IF NOT EXISTS itens_venda (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            venda_id INTEGER NOT NULL REFERENCES historico(id) ON DELETE CASCADE,
                            produto_id INTEGER REFERENCES prods(id) ON DELETE SET NULL,
                            nome TEXT NOT NULL,
                            qtd REAL NOT NULL,
                            preco_unit REAL NOT NULL DEFAULT 0,
                            total REAL NOT NULL DEFAULT 0)""")
        for sql in ("CREATE INDEX IF NOT EXISTS idx_prod_nome ON prods(nome)",
                    "CREATE INDEX IF NOT EXISTS idx_cli_nome ON clientes(nome)",
                    "CREATE INDEX IF NOT EXISTS idx_prod_norm ON prods(nome_norm)",
                    "CREATE INDEX IF NOT EXISTS idx_cli_norm ON clientes(nome_norm)",
                    "CREATE INDEX IF NOT EXISTS idx_hist_data_iso ON historico(data_iso)",
                    "CREATE INDEX IF NOT EXISTS idx_ent_data_iso ON entradas_estoque(data_iso)",
                    "CREATE INDEX IF NOT EXISTS idx_itens_venda ON itens_venda(venda_id)",
                    "CREATE INDEX IF NOT EXISTS idx_itens_prod ON itens_venda(produto_id)"):
            conn.execute(sql)
        conn.execute(f"PRAGMA user_version = {VERSAO_SCHEMA}")

    with conn:
        sincronizar_dados(conn)


def sincronizar_dados(conn):
    """Completa colunas novas em registros criados antes da migração (ou
    por uma versão antiga do programa). Seguro rodar sempre: só mexe no
    que está faltando."""
    for rid, nome, norm in conn.execute("SELECT id, nome, nome_norm FROM prods").fetchall():
        certo = normalizar_texto(nome)
        if norm != certo:
            conn.execute("UPDATE prods SET nome_norm=? WHERE id=?", (certo, rid))
    for rid, nome, norm, end, end_norm in conn.execute(
            "SELECT id, nome, nome_norm, endereco, endereco_norm FROM clientes").fetchall():
        certo, end_certo = normalizar_texto(nome), normalizar_texto(end)
        if norm != certo or end_norm != end_certo:
            conn.execute("UPDATE clientes SET nome_norm=?, endereco_norm=? WHERE id=?", (certo, end_certo, rid))

    def achar_produto(pid, nome):
        if pid and conn.execute("SELECT 1 FROM prods WHERE id=?", (pid,)).fetchone():
            return pid
        r = conn.execute("SELECT id FROM prods WHERE nome_norm=? LIMIT 1", (normalizar_texto(nome),)).fetchone()
        return r[0] if r else None

    pendentes = conn.execute("SELECT id, data, itens_json FROM historico WHERE data_iso IS NULL").fetchall()
    for vid, data, itens_json in pendentes:
        conn.execute("UPDATE historico SET data_iso=? WHERE id=?", (data_para_iso(data), vid))
        if conn.execute("SELECT 1 FROM itens_venda WHERE venda_id=? LIMIT 1", (vid,)).fetchone():
            continue
        for item in _carregar_json(itens_json):
            nome = str(item.get("n") or "").strip()
            qtd = _num(item.get("q"))
            if not nome:
                continue
            total = _num(item.get("t"))
            unit = _num(item.get("vu"), (total / qtd) if qtd else 0.0)
            conn.execute("INSERT INTO itens_venda (venda_id, produto_id, nome, qtd, preco_unit, total) VALUES (?,?,?,?,?,?)",
                         (vid, achar_produto(item.get("pid"), nome), nome, qtd, unit, total))

    for eid, nome, data, iso, pid in conn.execute(
            "SELECT id, nome_prod, data, data_iso, produto_id FROM entradas_estoque "
            "WHERE data_iso IS NULL OR produto_id IS NULL").fetchall():
        novo_iso = iso if iso is not None else data_para_iso(data)
        novo_pid = pid or achar_produto(None, nome)
        conn.execute("UPDATE entradas_estoque SET data_iso=?, produto_id=? WHERE id=?", (novo_iso, novo_pid, eid))


# =============================================================================
# BACKUP
# =============================================================================
def copiar_banco(conn, destino):
    """Cópia consistente usando a API de backup do SQLite."""
    dest = sqlite3.connect(destino)
    try:
        conn.backup(dest)
    finally:
        dest.close()


def assinatura_conteudo(conn):
    """'Impressão digital' do CONTEÚDO do banco (não do arquivo), usada
    para saber se algo mudou desde o último backup manual."""
    h = hashlib.sha256()
    for linha in conn.iterdump():
        h.update(linha.encode("utf-8"))
    return h.hexdigest()


def backup_manual(conn, pasta_backup):
    """Retorna (resultado, caminho_log). resultado: 'salvo' | 'sem_mudanca' | 'erro'."""
    os.makedirs(pasta_backup, exist_ok=True)
    caminho_hash = os.path.join(pasta_backup, "ultimo_hash.txt")
    caminho_log = os.path.join(pasta_backup, "log_backups.txt")
    agora = datetime.now()

    hash_anterior = None
    try:
        with open(caminho_hash, "r", encoding="utf-8") as f:
            hash_anterior = f.read().strip()
    except OSError:
        pass

    try:
        hash_atual = assinatura_conteudo(conn)
        if hash_atual == hash_anterior:
            resultado, linha = "sem_mudanca", "Verificado — nenhuma informação nova desde o último salvamento."
        else:
            nome = f"loja_backup_{agora:%Y-%m-%d_%H-%M-%S}.db"
            copiar_banco(conn, os.path.join(pasta_backup, nome))
            with open(caminho_hash, "w", encoding="utf-8") as f:
                f.write(hash_atual)
            resultado, linha = "salvo", f"Informações salvas com sucesso: {nome}"
    except Exception as e:
        resultado, linha = "erro", f"ERRO ao salvar backup: {e}"

    try:
        with open(caminho_log, "a", encoding="utf-8") as f:
            f.write(f"[{agora:%d/%m/%Y %H:%M:%S}] {linha}\n")
    except OSError:
        pass
    return resultado, caminho_log


def backup_diario(conn, pasta_backup):
    """Uma cópia por dia (na primeira abertura do dia)."""
    os.makedirs(pasta_backup, exist_ok=True)
    destino = os.path.join(pasta_backup, f"loja_backup_{datetime.now():%Y-%m-%d}.db")
    if not os.path.exists(destino):
        copiar_banco(conn, destino)


SUBPASTA_EXTERNA = "Backup Dois Irmaos"
COPIAS_EXTERNAS = 3
_RE_COPIA = re.compile(r"^(\d+)_loja_(.+)\.db$")
_LEIA_ME_EXTERNO = """BACKUP DO SISTEMA DOIS IRMAOS

1_loja_....db  = a copia MAIS RECENTE
2_loja_....db  = a anterior
3_loja_....db  = a mais antiga

A data e a hora no nome mostram quando cada copia foi feita.
Uma copia nova so e feita quando houve alguma mudanca (venda, cliente,
produto...). Para usar uma copia: feche o sistema, renomeie o arquivo
para loja.db e coloque-o na pasta do programa.
"""


def _copias_externas(pasta):
    """[(numero, nome_do_arquivo), ...] ordenado do mais recente (1) ao mais antigo."""
    achadas = []
    for nome in os.listdir(pasta):
        m = _RE_COPIA.match(nome)
        if m:
            achadas.append((int(m.group(1)), nome))
    return sorted(achadas)


def backup_externo(conn, pasta_escolhida, manter=COPIAS_EXTERNAS):
    """Cópia do banco para fora do PC (Google Drive, OneDrive, pendrive...).

    Na pasta escolhida cria 'Backup Dois Irmaos' com as 'manter' cópias mais
    recentes, numeradas: 1 = a mais nova. A cada cópia nova a mais antiga é
    apagada e as outras andam uma posição (1 -> 2 -> 3).
    Se nada mudou desde a última cópia, não envia nada.
    A cópia é feita num arquivo temporário e conferida antes de entrar na
    fila: o Drive nunca sincroniza um arquivo pela metade, e uma cópia com
    defeito nunca empurra as boas para fora.
    Retorna (resultado, caminho_da_copia_1, tamanho) com resultado
    'salvo' ou 'sem_mudanca'."""
    if not pasta_escolhida or not os.path.isdir(pasta_escolhida):
        raise FileNotFoundError(f"Pasta de backup não encontrada: {pasta_escolhida or '(nenhuma)'}\n"
                                "O pendrive/Drive está conectado?")
    pasta = os.path.join(pasta_escolhida, SUBPASTA_EXTERNA)
    os.makedirs(pasta, exist_ok=True)
    caminho_hash = os.path.join(pasta, "controle.txt")
    leia_me = os.path.join(pasta, "LEIA-ME.txt")
    if not os.path.exists(leia_me):
        with open(leia_me, "w", encoding="utf-8") as f:
            f.write(_LEIA_ME_EXTERNO)

    # Nada mudou desde a última cópia (e ela ainda está lá)? Não envia de novo.
    hash_atual = assinatura_conteudo(conn)
    copias = _copias_externas(pasta)
    try:
        with open(caminho_hash, "r", encoding="utf-8") as f:
            hash_anterior = f.read().strip()
    except OSError:
        hash_anterior = None
    if hash_atual == hash_anterior and copias and copias[0][0] == 1:
        atual = os.path.join(pasta, copias[0][1])
        return "sem_mudanca", atual, os.path.getsize(atual)

    agora = datetime.now()
    temp = os.path.join(pasta, f"~gravando_{agora:%H%M%S}.tmp")
    try:
        copiar_banco(conn, temp)
        # Confere a cópia antes de aceitar
        verif = sqlite3.connect(temp)
        try:
            ok = verif.execute("PRAGMA quick_check").fetchone()[0]
            vendas_copia = verif.execute("SELECT COUNT(*) FROM historico").fetchone()[0]
            # Arquivo único (sem -wal/-shm ao lado quando alguém abrir a cópia)
            verif.execute("PRAGMA journal_mode = DELETE")
        finally:
            verif.close()
        vendas = conn.execute("SELECT COUNT(*) FROM historico").fetchone()[0]
        if ok != "ok" or vendas_copia != vendas:
            raise RuntimeError(f"A cópia saiu com defeito ({ok}, {vendas_copia} de {vendas} vendas).")

        # Gira a fila: as que sairiam do limite são apagadas e as outras
        # recebem o número da posição seguinte (2, 3...), sem buracos mesmo
        # que alguém tenha apagado uma cópia à mão. Em duas etapas, para um
        # nome nunca sobrescrever outro.
        ficam, saem = copias[:max(manter - 1, 0)], copias[max(manter - 1, 0):]
        for _, nome in saem:
            os.remove(os.path.join(pasta, nome))
        intermediarios = []
        for posicao, (_, nome) in enumerate(ficam, start=2):
            data = _RE_COPIA.match(nome).group(2)
            meio = os.path.join(pasta, f"~girando_{posicao}.tmp")
            _substituir(os.path.join(pasta, nome), meio)
            intermediarios.append((meio, os.path.join(pasta, f"{posicao}_loja_{data}.db")))
        for meio, final in intermediarios:
            _substituir(meio, final)
        nova = os.path.join(pasta, f"1_loja_{agora:%Y-%m-%d_%Hh%M}.db")
        _substituir(temp, nova)
    finally:
        if os.path.exists(temp):
            try:
                os.remove(temp)
            except OSError:
                pass

    with open(caminho_hash, "w", encoding="utf-8") as f:
        f.write(hash_atual)
    return "salvo", nova, os.path.getsize(nova)


def _substituir(origem, destino, tentativas=10):
    """os.replace com novas tentativas: o Drive às vezes segura o arquivo
    alguns instantes enquanto sincroniza."""
    for i in range(tentativas):
        try:
            os.replace(origem, destino)
            return
        except PermissionError:
            if i == tentativas - 1:
                raise
            time.sleep(0.5)


# =============================================================================
# PRODUTOS
# =============================================================================
def buscar_produto(conn, nome):
    """(id, nome, preco, estoque) ou None — ignora acentos/maiúsculas."""
    norm = normalizar_texto(nome)
    if not norm:
        return None
    return conn.execute("SELECT id, nome, COALESCE(preco,0), COALESCE(estoque,0) FROM prods "
                        "WHERE nome_norm=? ORDER BY id LIMIT 1", (norm,)).fetchone()


def sugerir(conn, tabela, texto, limite=8):
    if tabela not in ("prods", "clientes"):
        return []
    norm = normalizar_texto(texto)
    if not norm:
        return []
    return [r[0] for r in conn.execute(
        f"SELECT nome FROM {tabela} WHERE nome_norm LIKE ? "
        f"ORDER BY CASE WHEN nome_norm LIKE ? THEN 0 ELSE 1 END, nome LIMIT ?",
        (f"%{norm}%", f"{norm}%", limite))]


def _intervalo_dia(dia=None):
    dia = dia or datetime.now()
    return dia.strftime("%Y-%m-%d 00:00"), dia.strftime("%Y-%m-%d 23:59")


def listar_produtos(conn, termo=""):
    """[(id, nome, preco, estoque, saidas_hoje)]"""
    ini, fim = _intervalo_dia()
    return conn.execute("""
        SELECT p.id, p.nome, COALESCE(p.preco,0), COALESCE(p.estoque,0), COALESCE(s.qtd,0)
          FROM prods p
          LEFT JOIN (SELECT iv.produto_id, SUM(iv.qtd) AS qtd
                       FROM itens_venda iv JOIN historico h ON h.id = iv.venda_id
                      WHERE h.data_iso BETWEEN ? AND ?
                      GROUP BY iv.produto_id) s ON s.produto_id = p.id
         WHERE p.nome_norm LIKE ?
         ORDER BY p.nome""", (ini, fim, f"%{normalizar_texto(termo)}%")).fetchall()


def salvar_produto(conn, id_edicao, nome, preco):
    """Retorna o nome do produto que já existe (se houver conflito) ou None."""
    norm = normalizar_texto(nome)
    dup = conn.execute("SELECT nome FROM prods WHERE nome_norm=? AND id IS NOT ? LIMIT 1", (norm, id_edicao)).fetchone()
    if dup:
        return dup[0]
    if id_edicao:
        conn.execute("UPDATE prods SET nome=?, nome_norm=?, preco=? WHERE id=?", (norm, norm, preco, id_edicao))
    else:
        conn.execute("INSERT INTO prods (nome, nome_norm, preco, estoque) VALUES (?,?,?,0)", (norm, norm, preco))
    return None


def excluir_produtos(conn, ids):
    conn.execute(f"DELETE FROM prods WHERE id IN ({','.join('?' * len(ids))})", ids)


def entrada_estoque(conn, nome, qtd):
    """Retorna (nome_real, novo_estoque) ou None se o produto não existir."""
    prod = buscar_produto(conn, nome)
    if not prod:
        return None
    pid, nome_real = prod[0], prod[1]
    agora = datetime.now()
    conn.execute("UPDATE prods SET estoque = COALESCE(estoque,0) + ? WHERE id=?", (qtd, pid))
    conn.execute("INSERT INTO entradas_estoque (nome_prod, produto_id, qtd, data, data_iso) VALUES (?,?,?,?,?)",
                 (nome_real, pid, qtd, agora.strftime(FORMATO_DATA), agora.strftime(FORMATO_ISO)))
    novo = conn.execute("SELECT estoque FROM prods WHERE id=?", (pid,)).fetchone()[0]
    return nome_real, novo


def listar_entradas(conn, termo="", f_data="", limite=300):
    """Histórico de chegadas de mercadoria: [(id, data, produto, qtd)]"""
    query = """SELECT e.id, e.data, COALESCE(p.nome, e.nome_prod), e.qtd
                 FROM entradas_estoque e LEFT JOIN prods p ON p.id = e.produto_id WHERE 1=1"""
    params = []
    norm = normalizar_texto(termo)
    if norm:
        query += " AND (p.nome_norm LIKE ? OR UPPER(e.nome_prod) LIKE ?)"; params += [f"%{norm}%", f"%{norm}%"]
    if f_data:
        query += " AND e.data LIKE ?"; params.append(f"%{f_data}%")
    query += " ORDER BY e.id DESC LIMIT ?"; params.append(limite)
    return conn.execute(query, params).fetchall()


def excluir_entradas(conn, ids):
    """Apaga entradas lançadas por engano e TIRA a quantidade do estoque."""
    marcadores = ",".join("?" * len(ids))
    for pid, qtd in conn.execute(f"SELECT produto_id, qtd FROM entradas_estoque WHERE id IN ({marcadores}) "
                                 f"AND produto_id IS NOT NULL", ids).fetchall():
        conn.execute("UPDATE prods SET estoque = COALESCE(estoque,0) - ? WHERE id=?", (qtd, pid))
    conn.execute(f"DELETE FROM entradas_estoque WHERE id IN ({marcadores})", ids)


def produtos_para_celular(conn):
    return conn.execute("SELECT nome, COALESCE(preco,0), COALESCE(estoque,0) FROM prods ORDER BY nome").fetchall()


# =============================================================================
# CLIENTES
# =============================================================================
def buscar_cliente(conn, nome):
    """(id, nome, endereco) ou None."""
    norm = normalizar_texto(nome)
    if not norm:
        return None
    return conn.execute("SELECT id, nome, COALESCE(endereco,'') FROM clientes WHERE nome_norm=? ORDER BY id LIMIT 1",
                        (norm,)).fetchone()


def listar_clientes(conn, termo="", limite=500):
    """Busca pelo NOME ou pelo ENDEREÇO (sem acentos). Quem bate no nome aparece primeiro."""
    norm = f"%{normalizar_texto(termo)}%"
    return conn.execute("SELECT id, nome, COALESCE(endereco,'') FROM clientes "
                        "WHERE nome_norm LIKE ? OR COALESCE(endereco_norm,'') LIKE ? "
                        "ORDER BY CASE WHEN nome_norm LIKE ? THEN 0 ELSE 1 END, nome LIMIT ?",
                        (norm, norm, norm, limite)).fetchall()


def _inserir_cliente(conn, nome, endereco):
    conn.execute("INSERT INTO clientes (nome, nome_norm, endereco, endereco_norm) VALUES (?,?,?,?)",
                 (nome, normalizar_texto(nome), endereco, normalizar_texto(endereco)))


def salvar_cliente(conn, id_edicao, nome, endereco):
    norm = normalizar_texto(nome)
    dup = conn.execute("SELECT nome FROM clientes WHERE nome_norm=? AND id IS NOT ? LIMIT 1", (norm, id_edicao)).fetchone()
    if dup:
        return dup[0]
    if id_edicao:
        conn.execute("UPDATE clientes SET nome=?, nome_norm=?, endereco=?, endereco_norm=? WHERE id=?",
                     (norm, norm, endereco, normalizar_texto(endereco), id_edicao))
    else:
        _inserir_cliente(conn, norm, endereco)
    return None


def excluir_clientes(conn, ids):
    conn.execute(f"DELETE FROM clientes WHERE id IN ({','.join('?' * len(ids))})", ids)


# =============================================================================
# VENDAS
# =============================================================================
def _ajustar_estoque_da_venda(conn, venda_id, sinal):
    for pid, qtd in conn.execute("SELECT produto_id, qtd FROM itens_venda WHERE venda_id=? AND produto_id IS NOT NULL",
                                 (venda_id,)).fetchall():
        conn.execute("UPDATE prods SET estoque = COALESCE(estoque,0) + ? WHERE id=?", (sinal * qtd, pid))


def registrar_venda(conn, venda):
    """venda = {cliente, endereco, forma, taxa, total, itens:[{pid,n,q,vu,t}], id_edicao}
    Retorna (venda_id, data_da_venda)."""
    cli, end = venda["cliente"], venda.get("endereco", "")
    if cli != "CONSUMIDOR":
        existente = buscar_cliente(conn, cli)
        if not existente:
            _inserir_cliente(conn, cli, end)
        elif end and not existente[2].strip():
            conn.execute("UPDATE clientes SET endereco=?, endereco_norm=? WHERE id=?", (end, normalizar_texto(end), existente[0]))

    itens = venda["itens"]
    itens_json = json.dumps(itens)
    id_edicao = venda.get("id_edicao")
    if id_edicao:
        r = conn.execute("SELECT data FROM historico WHERE id=?", (id_edicao,)).fetchone()
        if not r:
            raise ValueError(f"A venda #{id_edicao} não existe mais (foi excluída?).")
        data_venda = r[0]  # edição mantém a data original
        _ajustar_estoque_da_venda(conn, id_edicao, +1)
        conn.execute("DELETE FROM itens_venda WHERE venda_id=?", (id_edicao,))
        conn.execute("UPDATE historico SET cliente=?, total=?, forma=?, taxa=?, itens_json=? WHERE id=?",
                     (cli, venda["total"], venda["forma"], venda["taxa"], itens_json, id_edicao))
        venda_id = int(id_edicao)
    else:
        agora = datetime.now()
        data_venda = agora.strftime(FORMATO_DATA)
        cur = conn.execute("INSERT INTO historico (data, data_iso, cliente, total, forma, taxa, itens_json) VALUES (?,?,?,?,?,?,?)",
                           (data_venda, agora.strftime(FORMATO_ISO), cli, venda["total"], venda["forma"], venda["taxa"], itens_json))
        venda_id = cur.lastrowid

    for item in itens:
        pid = item.get("pid")
        if not (pid and conn.execute("SELECT 1 FROM prods WHERE id=?", (pid,)).fetchone()):
            achado = buscar_produto(conn, item.get("n"))
            pid = achado[0] if achado else None
        conn.execute("INSERT INTO itens_venda (venda_id, produto_id, nome, qtd, preco_unit, total) VALUES (?,?,?,?,?,?)",
                     (venda_id, pid, item["n"], _num(item.get("q")), _num(item.get("vu")), _num(item.get("t"))))
    _ajustar_estoque_da_venda(conn, venda_id, -1)
    return venda_id, data_venda


def excluir_vendas(conn, ids):
    """Apaga as vendas e devolve os itens ao estoque."""
    for vid in ids:
        _ajustar_estoque_da_venda(conn, vid, +1)
    marcadores = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM itens_venda WHERE venda_id IN ({marcadores})", ids)
    conn.execute(f"DELETE FROM historico WHERE id IN ({marcadores})", ids)


def ler_venda(conn, venda_id):
    r = conn.execute("SELECT id, data, cliente, COALESCE(total,0), COALESCE(forma,''), COALESCE(taxa,0) "
                     "FROM historico WHERE id=?", (venda_id,)).fetchone()
    if not r:
        return None
    itens = [{"pid": pid, "n": nome, "q": qtd, "vu": unit, "t": total}
             for pid, nome, qtd, unit, total in conn.execute(
                 "SELECT produto_id, nome, qtd, preco_unit, total FROM itens_venda WHERE venda_id=? ORDER BY id", (venda_id,))]
    cli = buscar_cliente(conn, r[2]) if r[2] else None
    return {"id": r[0], "data": r[1], "cliente": r[2] or "", "total": r[3], "forma": r[4], "taxa": r[5],
            "itens": itens, "endereco": cli[2] if cli else ""}


def listar_vendas(conn, f_id="", f_data="", f_cli="", limite=200):
    query = "SELECT id, data, cliente, COALESCE(total,0), COALESCE(forma,'') FROM historico WHERE 1=1"
    params = []
    if f_id:
        query += " AND id = ?"; params.append(int(f_id) if str(f_id).isdigit() else -1)
    if f_data:
        query += " AND data LIKE ?"; params.append(f"%{f_data}%")
    if f_cli:
        query += " AND cliente LIKE ?"; params.append(f"%{normalizar_texto(f_cli)}%")
    query += " ORDER BY id DESC LIMIT ?"; params.append(limite)
    return conn.execute(query, params).fetchall()


# =============================================================================
# RELATÓRIOS
# =============================================================================
def dados_periodo(conn, inicio, fim):
    """inicio/fim: datetime. Tudo calculado pelo SQL usando os índices."""
    ini, fi = inicio.strftime(FORMATO_ISO), fim.strftime(FORMATO_ISO)
    qtd_vendas, total_geral, total_taxas = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total),0), COALESCE(SUM(taxa),0) FROM historico WHERE data_iso BETWEEN ? AND ?",
        (ini, fi)).fetchone()

    # Vendas dia a dia (AAAA-MM-DD -> DD/MM/AAAA)
    por_dia = [(f"{d[8:10]}/{d[5:7]}/{d[0:4]}", q, v) for d, q, v in conn.execute(
        "SELECT substr(data_iso,1,10) AS dia, COUNT(*), COALESCE(SUM(total),0) FROM historico "
        "WHERE data_iso BETWEEN ? AND ? GROUP BY dia ORDER BY dia", (ini, fi))]

    financeiro = {}
    for forma, qtd, valor in conn.execute(
            "SELECT forma, COUNT(*), COALESCE(SUM(total),0) FROM historico "
            "WHERE data_iso BETWEEN ? AND ? AND COALESCE(forma,'') <> '' GROUP BY forma ORDER BY forma", (ini, fi)):
        financeiro[forma] = {"qtd": qtd, "valor": valor}

    vendidos = conn.execute("""
        SELECT MAX(COALESCE(p.nome, iv.nome)) AS nome, SUM(iv.qtd), SUM(iv.total)
          FROM itens_venda iv
          JOIN historico h ON h.id = iv.venda_id
          LEFT JOIN prods p ON p.id = iv.produto_id
         WHERE h.data_iso BETWEEN ? AND ?
         GROUP BY COALESCE('#' || iv.produto_id, iv.nome)
         ORDER BY nome""", (ini, fi)).fetchall()

    qtd_entradas = conn.execute("SELECT COUNT(*) FROM entradas_estoque WHERE data_iso BETWEEN ? AND ?", (ini, fi)).fetchone()[0]
    entradas = conn.execute("""
        SELECT MAX(COALESCE(p.nome, e.nome_prod)) AS nome, SUM(e.qtd)
          FROM entradas_estoque e
          LEFT JOIN prods p ON p.id = e.produto_id
         WHERE e.data_iso BETWEEN ? AND ?
         GROUP BY COALESCE('#' || e.produto_id, e.nome_prod)
         ORDER BY nome""", (ini, fi)).fetchall()

    return {
        "qtd_vendas": qtd_vendas,
        "total_geral": total_geral,
        "total_taxas": total_taxas,
        "ticket_medio": (total_geral / qtd_vendas) if qtd_vendas else 0.0,
        "vendas_por_dia": por_dia,
        "financeiro_por_forma": financeiro,
        # (produto, quantidade, valor vendido R$)
        "produtos_vendidos": [(n, float(q or 0), float(v or 0)) for n, q, v in vendidos],
        "qtd_entradas": qtd_entradas,
        "produtos_entrada": [(n, float(q or 0)) for n, q in entradas],
    }


# =============================================================================
# PAINEL (DASHBOARD)
# =============================================================================
_DIAS_SEMANA = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
_MESES_CURTOS = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]


def _indicadores(conn, ini, fi):
    qtd, total, taxas, entregas = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total),0), COALESCE(SUM(taxa),0), "
        "COALESCE(SUM(CASE WHEN COALESCE(taxa,0) > 0 THEN 1 ELSE 0 END),0) "
        "FROM historico WHERE data_iso BETWEEN ? AND ?", (ini, fi)).fetchone()
    itens = conn.execute("SELECT COALESCE(SUM(iv.qtd),0) FROM itens_venda iv JOIN historico h ON h.id = iv.venda_id "
                         "WHERE h.data_iso BETWEEN ? AND ?", (ini, fi)).fetchone()[0]
    return {"faturamento": total, "vendas": qtd, "ticket": (total / qtd) if qtd else 0.0,
            "itens": float(itens or 0), "entregas": entregas, "taxas": taxas}


def dados_painel(conn, inicio, fim, inicio_ant, fim_ant, granularidade):
    """Tudo que o painel mostra para o período [inicio, fim].
    granularidade: 'hora' | 'dia' | 'mes' (eixo do gráfico de faturamento).
    O período anterior serve para as comparações (▲▼) dos indicadores."""
    ini, fi = inicio.strftime(FORMATO_ISO), fim.strftime(FORMATO_ISO)

    # --- série no tempo (preenche com zero os pontos sem venda) ---
    if granularidade == "hora":
        chave_sql = "substr(data_iso,12,2)"
    elif granularidade == "dia":
        chave_sql = "substr(data_iso,1,10)"
    else:
        chave_sql = "substr(data_iso,1,7)"
    brutos = {k: (q, v) for k, q, v in conn.execute(
        f"SELECT {chave_sql} AS k, COUNT(*), COALESCE(SUM(total),0) FROM historico "
        f"WHERE data_iso BETWEEN ? AND ? GROUP BY k", (ini, fi))}

    serie = []  # (rótulo curto, valor R$, qtd vendas, rótulo longo)
    if granularidade == "hora":
        horas_com_venda = [int(h) for h in brutos]
        h_ini = min([7] + horas_com_venda)
        h_fim = max([19] + horas_com_venda)
        for h in range(h_ini, h_fim + 1):
            q, v = brutos.get(f"{h:02d}", (0, 0.0))
            serie.append((f"{h:02d}h", v, q, f"{h:02d}h às {h + 1:02d}h"))
    elif granularidade == "dia":
        d = inicio.replace(hour=0, minute=0, second=0, microsecond=0)
        while d.date() <= fim.date():
            q, v = brutos.get(d.strftime("%Y-%m-%d"), (0, 0.0))
            serie.append((d.strftime("%d/%m"), v, q, f"{d:%d/%m/%Y} ({_DIAS_SEMANA[d.weekday()]})"))
            d += timedelta(days=1)
    else:
        ano, mes = inicio.year, inicio.month
        while (ano, mes) <= (fim.year, fim.month):
            q, v = brutos.get(f"{ano:04d}-{mes:02d}", (0, 0.0))
            serie.append((f"{_MESES_CURTOS[mes - 1]}/{ano % 100:02d}", v, q, f"{_MESES_CURTOS[mes - 1]}/{ano}"))
            mes += 1
            if mes > 12:
                ano, mes = ano + 1, 1

    formas = conn.execute(
        "SELECT CASE WHEN COALESCE(forma,'') = '' THEN 'Sem forma' ELSE forma END AS f, COUNT(*), COALESCE(SUM(total),0) "
        "FROM historico WHERE data_iso BETWEEN ? AND ? GROUP BY f ORDER BY 3 DESC", (ini, fi)).fetchall()

    produtos = conn.execute("""
        SELECT MAX(COALESCE(p.nome, iv.nome)) AS nome, SUM(iv.qtd) AS q, SUM(iv.total)
          FROM itens_venda iv
          JOIN historico h ON h.id = iv.venda_id
          LEFT JOIN prods p ON p.id = iv.produto_id
         WHERE h.data_iso BETWEEN ? AND ?
         GROUP BY COALESCE('#' || iv.produto_id, iv.nome)
         ORDER BY q DESC, nome""", (ini, fi)).fetchall()

    return {
        "atual": _indicadores(conn, ini, fi),
        "anterior": _indicadores(conn, inicio_ant.strftime(FORMATO_ISO), fim_ant.strftime(FORMATO_ISO)),
        "granularidade": granularidade,
        "serie": serie,
        "formas": [(f, q, float(v)) for f, q, v in formas],
        "produtos": [(n, float(q or 0), float(v or 0)) for n, q, v in produtos],
    }


# =============================================================================
# THREAD DO BANCO
# =============================================================================
class Banco:
    """Executa as operações acima numa única thread dedicada.

    despachar(funcao, *args): como entregar o resultado na thread da tela.
    """

    def __init__(self, caminho, despachar, ao_erro, pasta_backup=None):
        self.caminho = caminho
        self._despachar = despachar
        self._ao_erro = ao_erro
        conn = conectar(caminho)
        # WAL: o servidor do celular lê enquanto o sistema grava, sem um travar o outro
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error:
            pass
        preparar_banco(conn, pasta_backup)
        if pasta_backup:
            try:
                backup_diario(conn, pasta_backup)
            except Exception as e:
                print(f"Aviso: backup diário não foi feito: {e}")
        self._conn = conn
        self._fila = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="banco", daemon=True)
        self._thread.start()

    def enviar(self, funcao, *args, ok=None, erro=None):
        """Agenda funcao(conn, *args). 'ok(resultado)' / 'erro(exc)' rodam na thread da tela."""
        self._fila.put((funcao, args, ok, erro))

    def ocioso(self):
        return self._fila.unfinished_tasks == 0

    def _loop(self):
        while True:
            tarefa = self._fila.get()
            try:
                if tarefa is None:
                    break
                funcao, args, ok, erro = tarefa
                try:
                    with self._conn:  # transação: grava tudo ou nada
                        resultado = funcao(self._conn, *args)
                except Exception as exc:
                    self._despachar(erro or self._ao_erro, exc)
                    continue
                if ok is not None:
                    self._despachar(ok, resultado)
            finally:
                self._fila.task_done()
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._conn.close()

    def fechar(self, espera=5):
        self._fila.put(None)
        self._thread.join(timeout=espera)
