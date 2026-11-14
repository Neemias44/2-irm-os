import customtkinter as ctk
import tkinter as tk
import json
import os
import queue
import sys
import tempfile
import threading
import socket
import textwrap
from datetime import datetime, timedelta
from tkinter import messagebox, filedialog, ttk

import banco as bd
from banco import normalizar_texto, FORMATO_DATA
from painel import Painel, moeda
from vigia import VigiaTravamentos

# --- TENTA IMPORTAR O REPORTLAB (PDF) ---
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    TEM_REPORTLAB = True
except ImportError:
    TEM_REPORTLAB = False
    print("AVISO: Para gerar PDF, instale: pip install reportlab")

# --- TENTA IMPORTAR O FLASK PARA O CELULAR ---
try:
    from flask import Flask, render_template_string
    TEM_FLASK = True
except ImportError:
    TEM_FLASK = False
    print("AVISO: Instale o flask para ver no celular: pip install flask")

# --- CONFIGURAÇÃO VISUAL ---
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# --- IMPORTAÇÃO IMPRESSORA ---
try:
    import win32print
    TEM_IMPRESSORA = True
except ImportError:
    TEM_IMPRESSORA = False


# --- CONSTANTES ---
# Tudo fica na pasta do programa (do .exe, quando empacotado), e NÃO na pasta
# "de onde foi aberto": um atalho com outra pasta em "Iniciar em" criaria um
# banco vazio e pareceria que tudo sumiu.
PASTA_APP = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
ARQUIVO_DB = os.path.join(PASTA_APP, "loja.db")
PASTA_BACKUP = os.path.join(PASTA_APP, "backups")
ARQUIVO_CONFIG = os.path.join(PASTA_APP, "config.json")
ARQUIVO_LOG_TRAVAS = os.path.join(PASTA_APP, "travamentos.log")
ESTOQUE_MINIMO = 10      # abaixo disso o produto aparece em vermelho (PC e celular)

CORES_STATUS = {"ok": "#4CAF50", "aviso": "#FFC107", "erro": "#ff5555", "info": "#9e9e9e"}


class Autocomplete:
    """Sugestões para um campo de texto.

    A lista é um tk.Listbox desenhado DENTRO da janela principal (não é
    uma janela separada), então não rouba o foco, não abre atrás de nada
    e é leve de redesenhar. Teclado: ↓/↑ escolhe, Enter confirma, Esc fecha.
    A busca roda na thread do banco: digitar nunca trava a tela.
    """

    def __init__(self, app, campo, tabela, ao_escolher=None, ao_enter=None):
        self.app, self.campo, self.tabela = app, campo, tabela
        self.ao_escolher = ao_escolher
        self.ao_enter = ao_enter       # Enter sem lista aberta
        self._job = None
        self._seq = 0
        campo.bind("<KeyRelease>", self._tecla)
        campo.bind("<Down>", lambda e: self._mover(+1))
        campo.bind("<Up>", lambda e: self._mover(-1))
        campo.bind("<Return>", self._enter)
        campo.bind("<KP_Enter>", self._enter)
        campo.bind("<Escape>", lambda e: self.esconder())
        campo.bind("<FocusOut>", lambda e: self.app.after(200, self._fechar_se_sem_foco))

    @property
    def aberto(self):
        return self.app._ac_ativo is self

    def _tecla(self, event):
        if event.keysym in ("Up", "Down", "Return", "KP_Enter", "Escape", "Tab", "Shift_L", "Shift_R",
                            "Control_L", "Control_R", "Alt_L", "Alt_R", "Left", "Right", "Home", "End"):
            return
        if self._job is not None:
            self.app.after_cancel(self._job)
        self._job = self.app.after(150, self._buscar)

    def _buscar(self):
        self._job = None
        texto = self.campo.get()
        if not normalizar_texto(texto):
            return self.esconder()
        self._seq += 1
        seq = self._seq
        self.app.banco.enviar(bd.sugerir, self.tabela, texto, ok=lambda res: self._mostrar(res, seq, texto))

    def _mostrar(self, sugestoes, seq, texto):
        # Ignora respostas velhas (o usuário já digitou outra coisa)
        if seq != self._seq or self.campo.get() != texto:
            return
        if not sugestoes or self.app.focus_get() is not self.campo._entry:
            return self.esconder()
        self.app._mostrar_lista_sugestoes(self, sugestoes)

    def _mover(self, passo):
        if not self.aberto:
            return
        lb = self.app.lista_ac
        atual = lb.curselection()
        i = (atual[0] + passo) if atual else 0
        i = max(0, min(lb.size() - 1, i))
        lb.selection_clear(0, "end"); lb.selection_set(i); lb.see(i)
        return "break"

    def _enter(self, event=None):
        if self.aberto:
            sel = self.app.lista_ac.curselection()
            if sel:
                self.escolher(self.app.lista_ac.get(sel[0]))
                return "break"
        self.esconder()
        if self.ao_enter:
            self.ao_enter()
        return "break"

    def escolher(self, valor):
        if self._job is not None:
            self.app.after_cancel(self._job); self._job = None
        self._seq += 1  # descarta buscas que ainda estejam a caminho
        self.campo.delete(0, "end"); self.campo.insert(0, valor)
        self.esconder()
        if self.ao_escolher:
            self.ao_escolher(valor)

    def esconder(self):
        if self.aberto:
            self.app._esconder_lista_sugestoes()

    def _fechar_se_sem_foco(self):
        if self.app.focus_get() is not self.campo._entry:
            self.esconder()


class SistemaComanda(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Sistema Dois Irmãos - Versão Final Engenheiro")
        self.geometry("1280x850")

        # Variáveis de Estado
        self.id_cliente_em_edicao = None
        self.id_prod_em_edicao = None
        self.id_venda_em_edicao = None
        self.carrinho = []
        self.total_itens = 0.0
        self.ip_local = self.pegar_ip()
        self._salvando_venda = False
        self._refresh_jobs = {}
        self._seq_listas = {}
        self._status_job = None
        self._ac_ativo = None
        self._editor_carrinho = None

        # Fila de tarefas de tela: outras threads colocam funções aqui e a
        # thread principal as executa (o tkinter só pode ser usado por ela).
        self._fila_ui = queue.Queue()
        self.after(30, self._processar_fila_ui)

        # Registra em travamentos.log qualquer parada da tela > 0,5 s e erros silenciosos
        self.vigia = VigiaTravamentos(self, arquivo=ARQUIVO_LOG_TRAVAS, limite=0.5)
        self.config_app = self._ler_config()

        # Banco de dados (migra/atualiza a estrutura e faz o backup do dia)
        try:
            self.banco = bd.Banco(ARQUIVO_DB, despachar=self._na_ui, ao_erro=self._erro_banco, pasta_backup=PASTA_BACKUP)
        except Exception as e:
            messagebox.showerror("Erro de Banco", f"Não foi possível abrir o banco de dados:\n{e}")
            self.destroy()
            raise SystemExit(1)

        self.estilizar_treeview()
        if TEM_FLASK:
            self.iniciar_servidor_celular()

        # Barra de status (avisos que NÃO travam a tela)
        self.barra_status = ctk.CTkFrame(self, height=34, corner_radius=0, fg_color="#1e1e1e")
        self.barra_status.pack(side="bottom", fill="x")
        self.lbl_status = ctk.CTkLabel(self.barra_status, text="Pronto.", anchor="w", font=("Arial", 14, "bold"),
                                       text_color=CORES_STATUS["info"])
        self.lbl_status.pack(side="left", fill="x", expand=True, padx=12, pady=4)

        # --- ABAS ---
        self.abas = ctk.CTkTabview(self)
        self.abas.pack(padx=10, pady=(10, 0), fill="both", expand=True)
        self.tab_venda = self.abas.add("Venda")
        self.tab_clientes = self.abas.add("Clientes")
        self.tab_produtos = self.abas.add("Estoque")
        self.tab_entrada = self.abas.add("Entrada de Mercadoria")
        self.tab_historico = self.abas.add("Histórico de Vendas")
        self.tab_relatorio = self.abas.add("Relatórios")

        # Lista de sugestões (uma só, reaproveitada por todos os campos)
        self.lista_ac = tk.Listbox(self, bg="#333333", fg="white", selectbackground="#1f6aa5", selectforeground="white",
                                   font=("Arial", 12), activestyle="none", highlightthickness=1,
                                   highlightbackground="#1f6aa5", borderwidth=0, exportselection=False, takefocus=0)
        self.lista_ac.bind("<ButtonRelease-1>", self._clique_lista_sugestoes)
        self.lista_ac.bind("<Motion>", self._hover_lista_sugestoes)
        self.bind("<Button-1>", self._clique_global)

        self.montar_aba_venda()
        self.montar_aba_clientes()
        self.montar_aba_produtos()
        self.montar_aba_entrada()
        self.montar_aba_historico()
        self.montar_aba_relatorio()

        self.protocol("WM_DELETE_WINDOW", self._ao_fechar)
        # PC desligado sem fechar o programa? Faz o backup externo que ficou faltando.
        self.after(4000, self._backup_externo_pendente)

    # =========================================================================
    # INFRAESTRUTURA DE TELA
    # =========================================================================
    def _na_ui(self, funcao, *args):
        """Agenda 'funcao(*args)' na thread da tela. Pode ser chamada de qualquer thread."""
        self._fila_ui.put((funcao, args))

    def _processar_fila_ui(self):
        try:
            while True:
                funcao, args = self._fila_ui.get_nowait()
                try:
                    funcao(*args)
                except Exception as e:
                    print(f"Erro em tarefa de tela: {e}")
        except queue.Empty:
            pass
        self.after(30, self._processar_fila_ui)

    def _erro_banco(self, exc):
        messagebox.showerror("Erro", str(exc))

    def notificar(self, texto, tipo="ok", segundos=8):
        """Mensagem na barra de baixo. Não precisa clicar em OK."""
        self.lbl_status.configure(text=texto, text_color=CORES_STATUS.get(tipo, CORES_STATUS["info"]))
        if self._status_job is not None:
            self.after_cancel(self._status_job)
        self._status_job = self.after(segundos * 1000, lambda: self.lbl_status.configure(text="", text_color=CORES_STATUS["info"]))

    def _em_segundo_plano(self, funcao, *args, ok=None, erro_titulo="Erro"):
        """Roda trabalho que não é de banco (impressora, montar PDF) sem travar a tela."""
        def worker():
            try:
                resultado = funcao(*args)
            except Exception as e:
                msg = str(e)
                self._na_ui(messagebox.showerror, erro_titulo, msg)
                return
            if ok is not None:
                self._na_ui(ok, resultado)
        threading.Thread(target=worker, daemon=True).start()

    def _ao_fechar(self):
        if self._salvando_venda and not messagebox.askyesno("Sair", "Uma venda ainda está sendo salva. Sair mesmo assim?"):
            return
        # Fim do dia: manda o backup para o Drive/pendrive antes de fechar
        if self.config_app.get("pasta_backup_externo"):
            self.notificar("☁️ Salvando backup no Drive antes de fechar...", "info")
            ok, resultado = self._executar_e_esperar(bd.backup_externo, self.config_app["pasta_backup_externo"])
            if ok:
                self._registrar_backup_externo(resultado)
            elif not messagebox.askyesno(
                    "Backup não foi feito",
                    f"Não foi possível salvar o backup no Drive/pendrive:\n\n{resultado}\n\n"
                    "Fechar o programa mesmo assim?\n(Os dados continuam salvos neste computador.)",
                    icon="warning"):
                return
        self.banco.fechar()
        self.destroy()

    def _executar_e_esperar(self, funcao, *args, limite=120):
        """Roda funcao na thread do banco e espera o resultado mantendo a
        janela viva (usado só ao fechar, quando não dá para seguir adiante
        sem a resposta). Retorna (True, resultado) ou (False, mensagem)."""
        pronto = threading.Event()
        caixa = {}

        def tarefa(conn, *a):
            try:
                caixa["r"] = (True, funcao(conn, *a))
            except Exception as e:
                caixa["r"] = (False, str(e))
            finally:
                pronto.set()

        self.banco.enviar(tarefa, *args)
        fim = datetime.now() + timedelta(seconds=limite)
        while not pronto.wait(0.05):
            if datetime.now() > fim:
                return False, "O Drive/pendrive demorou demais para responder."
            self.update()
        return caixa["r"]

    # =========================================================================
    # BACKUP EXTERNO (Google Drive / OneDrive / pendrive)
    # =========================================================================
    def _ler_config(self):
        try:
            with open(ARQUIVO_CONFIG, "r", encoding="utf-8") as f:
                dados = json.load(f)
                return dados if isinstance(dados, dict) else {}
        except (OSError, ValueError):
            return {}

    def _salvar_config(self):
        try:
            temp = ARQUIVO_CONFIG + ".tmp"
            with open(temp, "w", encoding="utf-8") as f:
                json.dump(self.config_app, f, ensure_ascii=False, indent=2)
            os.replace(temp, ARQUIVO_CONFIG)
        except OSError as e:
            self.notificar(f"Não foi possível salvar a configuração: {e}", "erro")

    def escolher_pasta_backup_externo(self):
        messagebox.showinfo(
            "Pasta do backup",
            "Escolha a pasta onde o backup será guardado FORA deste computador:\n\n"
            "• Google Drive: normalmente  G:\\Meu Drive\n"
            "• OneDrive: a pasta OneDrive do usuário\n"
            "• Pendrive: a letra do pendrive (ex.: E:\\)\n\n"
            "O sistema cria lá dentro a pasta \"Backup Dois Irmaos\".")
        pasta = filedialog.askdirectory(title="Pasta do backup (Drive ou pendrive)",
                                        initialdir=self.config_app.get("pasta_backup_externo") or os.path.expanduser("~"))
        if not pasta:
            return False
        pasta = os.path.normpath(pasta)
        if os.path.normcase(pasta).startswith(os.path.normcase(PASTA_APP)):
            messagebox.showwarning("Pasta do backup", "Essa pasta fica dentro do próprio sistema, no mesmo computador.\n"
                                                      "Escolha o Google Drive, o OneDrive ou um pendrive.")
            return False
        self.config_app["pasta_backup_externo"] = pasta
        self._salvar_config()
        self.notificar(f"Pasta do backup: {pasta}", "ok")
        return True

    def backup_externo_agora(self):
        if not self.config_app.get("pasta_backup_externo") and not self.escolher_pasta_backup_externo():
            return
        self.btn_drive.configure(state="disabled")
        self.notificar("☁️ Salvando backup no Drive...", "info")
        self.banco.enviar(bd.backup_externo, self.config_app["pasta_backup_externo"],
                          ok=self._backup_externo_ok, erro=self._backup_externo_erro)

    def _backup_externo_ok(self, resultado):
        self.btn_drive.configure(state="normal")
        self._registrar_backup_externo(resultado)

    def _backup_externo_erro(self, exc):
        self.btn_drive.configure(state="normal")
        self.notificar("❌ Backup no Drive NÃO foi feito (veja a mensagem)", "erro")
        if messagebox.askyesno("Backup no Drive", f"O backup não foi feito:\n\n{exc}\n\nEscolher outra pasta?", icon="warning"):
            if self.escolher_pasta_backup_externo():
                self.backup_externo_agora()

    def _registrar_backup_externo(self, resultado):
        situacao, caminho, tamanho = resultado
        self.config_app["ultimo_backup_externo"] = datetime.now().strftime(FORMATO_DATA)
        self._salvar_config()
        if situacao == "sem_mudanca":
            self.notificar(f"✅ Backup no Drive já está em dia (nada mudou desde a última cópia): "
                           f"{os.path.basename(caminho)}", "ok")
        else:
            self.notificar(f"✅ Backup no Drive salvo: {os.path.basename(caminho)}  ({tamanho / 1024 / 1024:.1f} MB)", "ok")

    def _backup_externo_pendente(self):
        pasta = self.config_app.get("pasta_backup_externo")
        if not pasta:
            self.notificar("⚠️ Backup no Drive ainda não configurado: clique em ☁️ BACKUP NO DRIVE", "aviso")
            return
        ultimo = self.config_app.get("ultimo_backup_externo", "")
        if ultimo.startswith(datetime.now().strftime("%d/%m/%Y")):
            self.notificar(f"Último backup no Drive: {ultimo}", "info")
            return
        # Ainda não houve backup externo hoje (ex.: o PC foi desligado sem fechar o
        # programa ontem). Faz agora, em segundo plano; se falhar, só avisa na barra.
        self.banco.enviar(bd.backup_externo, pasta, ok=self._registrar_backup_externo,
                          erro=lambda exc: self.notificar(
                              f"⚠️ Backup no Drive não foi feito ({exc}). Último: {ultimo or 'nunca'}", "aviso"))

    def _agendar_refresh_lista(self, chave, callback, delay=150):
        job = self._refresh_jobs.get(chave)
        if job is not None:
            self.after_cancel(job)
        self._refresh_jobs[chave] = self.after(delay, callback)

    def _agendar_refresh_geral(self):
        self.atualizar_lista_produtos()
        self.atualizar_lista_clientes()
        self.atualizar_lista_vendas()
        self.atualizar_lista_entradas()
        self.atualizar_resumo_periodo()

    def _novo_seq(self, chave):
        self._seq_listas[chave] = self._seq_listas.get(chave, 0) + 1
        return self._seq_listas[chave]

    # --- Lista de sugestões ---
    def _mostrar_lista_sugestoes(self, ac, sugestoes):
        lb = self.lista_ac
        lb.delete(0, "end")
        for s in sugestoes:
            lb.insert("end", s)
        lb.selection_set(0)
        lb.configure(height=len(sugestoes))
        campo = ac.campo
        x = campo.winfo_rootx() - self.winfo_rootx()
        y = campo.winfo_rooty() - self.winfo_rooty() + campo.winfo_height()
        lb.place(x=x, y=y, width=campo.winfo_width())
        lb.lift()
        self._ac_ativo = ac

    def _esconder_lista_sugestoes(self):
        self.lista_ac.place_forget()
        self._ac_ativo = None

    def _hover_lista_sugestoes(self, event):
        i = self.lista_ac.nearest(event.y)
        self.lista_ac.selection_clear(0, "end"); self.lista_ac.selection_set(i)

    def _clique_lista_sugestoes(self, event):
        if self._ac_ativo is None or self.lista_ac.size() == 0:
            return
        self._ac_ativo.escolher(self.lista_ac.get(self.lista_ac.nearest(event.y)))

    def _clique_global(self, event):
        if event.widget is not self.lista_ac and self._ac_ativo is not None:
            if event.widget is not self._ac_ativo.campo._entry:
                self._esconder_lista_sugestoes()

    # --- ESTILO DARK PARA TABELAS (Treeview) ---
    def estilizar_treeview(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b",
                        rowheight=30, bordercolor="#2b2b2b", borderwidth=0, font=("Arial", 11))
        style.map("Treeview", background=[("selected", "#1f6aa5")])
        style.configure("Treeview.Heading", background="#333333", foreground="white", relief="flat",
                        font=("Arial", 10, "bold"))
        style.map("Treeview.Heading", background=[("active", "#444444")])

    # =========================================================================
    # UTILITÁRIOS
    # =========================================================================
    def safe_float(self, valor):
        try:
            if valor is None:
                return 0.0
            if isinstance(valor, (int, float)):
                return float(valor)
            texto = str(valor).replace("R$", "").replace(" ", "").strip()
            if texto == "":
                return 0.0
            if "," in texto and "." in texto:
                if texto.rfind(",") > texto.rfind("."):
                    texto = texto.replace(".", "").replace(",", ".")
                else:
                    texto = texto.replace(",", "")
            elif "," in texto:
                texto = texto.replace(",", ".")
            return float(texto)
        except Exception:
            return 0.0

    def safe_int(self, valor):
        return int(self.safe_float(valor))

    def _formatar_qtd(self, qtd):
        qtd = self.safe_float(qtd)
        return f"{qtd:.0f}" if qtd.is_integer() else f"{qtd:.2f}"

    def pegar_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _ids_selecionados(self, tree):
        return [self.safe_int(tree.item(i, "values")[0]) for i in tree.selection()]

    # =========================================================================
    # BACKUP MANUAL
    # =========================================================================
    def salvar_backup_manual(self):
        self.notificar("💾 Salvando informações...", "info")
        self.banco.enviar(bd.backup_manual, PASTA_BACKUP, ok=lambda r: self._mostrar_janela_log_backup(*r))

    def _mostrar_janela_log_backup(self, resultado, caminho_log):
        if resultado == "salvo":
            titulo, cor = "✅ Informações salvas com sucesso!", "#4CAF50"
        elif resultado == "sem_mudanca":
            titulo, cor = "ℹ️ Nada de diferente — nenhuma alteração encontrada", "#FF9800"
        else:
            titulo, cor = "❌ Erro ao salvar", "#f44336"
        self.notificar(titulo, "erro" if resultado == "erro" else "ok")

        janela = ctk.CTkToplevel(self)
        janela.title("Salvar Informações")
        janela.geometry("560x420")
        janela.transient(self)
        janela.after(50, janela.lift)

        ctk.CTkLabel(janela, text=titulo, font=("Arial", 16, "bold"), text_color=cor).pack(pady=(15, 10))
        ctk.CTkLabel(janela, text="Histórico (log) de salvamentos:", text_color="gray").pack(anchor="w", padx=15)
        caixa = ctk.CTkTextbox(janela, width=520, height=280)
        caixa.pack(padx=15, pady=5, fill="both", expand=True)
        try:
            with open(caminho_log, "r", encoding="utf-8") as f:
                texto_log = "".join(f.readlines()[-30:])
        except OSError:
            texto_log = "(sem histórico ainda)"
        caixa.insert("1.0", texto_log)
        caixa.see("end")
        caixa.configure(state="disabled")
        ctk.CTkButton(janela, text="Fechar", command=janela.destroy).pack(pady=10)

    # =========================================================================
    # SERVIDOR CELULAR (FLASK)
    # =========================================================================
    def iniciar_servidor_celular(self):
        if getattr(self, '_servidor_celular_ativo', False):
            return
        self._servidor_celular_ativo = True
        app = Flask(__name__)
        HTML_TEMPLATE = """
        <!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Estoque Dois Irmãos</title><meta http-equiv="refresh" content="30">
        <style>body{font-family:Arial,sans-serif;background:#222;color:#fff;padding:10px} .card{background:#333;padding:15px;margin-bottom:10px;border-left:5px solid #2196F3;display:flex;justify-content:space-between} .low{border-left-color:#f44336} .val{font-size:1.4em;font-weight:bold}</style>
        </head><body><h2 style="text-align:center;color:#4caf50">📦 Estoque</h2>
        <p style="text-align:center;color:#aaa">Valor em mercadoria: <b>R$ {{ "%.2f"|format(total_loja) }}</b></p>
        {% for p in produtos %}
        <div class="card {% if p[2] < minimo %}low{% endif %}"><div><b>{{p[0]}}</b><br><small>R$ {{ "%.2f"|format(p[1]) }}</small></div><div style="text-align:right"><span class="val">{{ "%g"|format(p[2]) }}</span><br><small>un</small></div></div>
        {% endfor %}</body></html>
        """

        @app.route("/")
        def index():
            # Conexão própria só de leitura: com o modo WAL ela não bloqueia o sistema
            try:
                conn = bd.conectar(ARQUIVO_DB)
                try:
                    dados = bd.produtos_para_celular(conn)
                finally:
                    conn.close()
                total_loja = sum(preco * max(estoque, 0) for _, preco, estoque in dados)
                return render_template_string(HTML_TEMPLATE, produtos=dados, minimo=ESTOQUE_MINIMO, total_loja=total_loja)
            except Exception as e:
                print(f"Erro no servidor do celular: {e}")
                return "Erro ao ler o banco de dados", 500

        def run_server():
            try:
                app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
            except OSError as e:
                print(f"Aviso: servidor do celular não iniciou: {e}")
                self._servidor_celular_ativo = False

        threading.Thread(target=run_server, daemon=True).start()

    def mostrar_info_celular(self):
        if not TEM_FLASK:
            return messagebox.showwarning("Aviso", "Instale o flask: pip install flask")
        messagebox.showinfo("Acesso Celular", f"Acesse no navegador do celular:\nhttp://{self.ip_local}:5000")

    # =========================================================================
    # IMPRESSÃO (TÉRMICA) — sempre em segundo plano: impressora desligada
    # ou travada não congela mais a tela.
    # =========================================================================
    def imprimir(self, texto):
        aviso = "🖨 Enviando para a impressora..."
        self.notificar(aviso, "info")

        def enviado(_):
            # Não apaga uma mensagem mais importante (ex.: "Venda #12 salva")
            if self.lbl_status.cget("text") == aviso:
                self.notificar("🖨 Impressão enviada.", "ok")

        self._em_segundo_plano(self._enviar_para_impressora, texto, erro_titulo="Erro Impressão", ok=enviado)

    def _enviar_para_impressora(self, texto):
        if not TEM_IMPRESSORA:
            with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
                f.write(texto)
            os.startfile(f.name)
            return
        impressora_padrao = win32print.GetDefaultPrinter()
        hPrinter = win32print.OpenPrinter(impressora_padrao)
        try:
            win32print.StartDocPrinter(hPrinter, 1, ("Sistema Dois Irmaos", None, "RAW"))
            try:
                win32print.StartPagePrinter(hPrinter)
                win32print.WritePrinter(hPrinter, bytes(texto, "latin-1", errors="replace"))
                win32print.WritePrinter(hPrinter, b'\n\n\n\n\x1d\x56\x00')  # Corte
                win32print.EndPagePrinter(hPrinter)
            finally:
                win32print.EndDocPrinter(hPrinter)
        finally:
            win32print.ClosePrinter(hPrinter)

    def gerar_layout_cupom(self, id_venda, cliente, endereco_cliente, carrinho, subtotal, taxa, total, pgto,
                           valor_recebido=0.0, data_hora=None, reimpressao=False):
        data_hora = data_hora or datetime.now().strftime(FORMATO_DATA)
        id_venda = self.safe_int(id_venda)
        cliente = cliente or "CONSUMIDOR"
        LARGURA = 48
        traco = "=" * LARGURA

        txt = "\n" + "DOIS IRMAOS - AGUA E GAS".center(LARGURA) + "\n"
        txt += f"{traco}\nVENDA #{id_venda:04d} - {data_hora}\n"

        prefixo_cli = "Cli: "
        for i, l in enumerate(textwrap.wrap(cliente, width=LARGURA - len(prefixo_cli))):
            txt += f"{prefixo_cli if i == 0 else ' ' * len(prefixo_cli)}{l}\n"
        if endereco_cliente:
            prefixo_end = "End: "
            for i, l in enumerate(textwrap.wrap(endereco_cliente, width=LARGURA - len(prefixo_end))):
                txt += f"{prefixo_end if i == 0 else ' ' * len(prefixo_end)}{l}\n"

        txt += f"{traco}\n{'PRODUTO':<28} {'QTD':^5} {'TOTAL':>13}\n"
        for item in carrinho:
            nome_prod = str(item.get('n', ''))[:28]
            txt += f"{nome_prod:<28} {self._formatar_qtd(item.get('q', 0)):^5} {float(item.get('t', 0)):>13.2f}\n"

        txt += f"{traco}\n"
        if taxa:
            txt += f"SUBTOTAL: {subtotal:>38.2f}\n"
            txt += f"TAXA DE ENTREGA: {taxa:>31.2f}\n"
        txt += f"TOTAL A PAGAR: {total:>33.2f}\nPagamento: {pgto}\n"
        if pgto == "Dinheiro" and valor_recebido > 0:
            txt += f"RECEBIDO: {valor_recebido:>38.2f}\n"
            txt += f"TROCO: {(valor_recebido - total):>41.2f}\n"
        if reimpressao:
            txt += "\n" + "*** 2a VIA ***".center(LARGURA) + "\n"
        txt += "\n" + "OBRIGADO PELA PREFERENCIA!".center(LARGURA) + "\n\n\n"
        return txt

    # =========================================================================
    # RELATÓRIOS POR PERÍODO (DIÁRIO / SEMANAL / MENSAL / ANUAL / DATAS LIVRES)
    # Os números vêm do banco (thread do banco) e o PDF é montado em outra
    # thread: a tela continua livre durante todo o processo.
    # =========================================================================
    TIPOS_PERIODO = ["Diário", "Semanal", "Mensal", "Anual", "Personalizado"]
    MESES = ["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto",
             "Setembro", "Outubro", "Novembro", "Dezembro"]

    # --- Escolha do período ---
    @staticmethod
    def _limites_periodo(tipo, ref):
        """Primeiro e último dia do período que contém a data 'ref'.
        Semana = segunda a domingo."""
        d = ref.replace(hour=0, minute=0, second=0, microsecond=0)
        if tipo == "Semanal":
            ini = d - timedelta(days=d.weekday())
            fim = ini + timedelta(days=6)
        elif tipo == "Mensal":
            ini = d.replace(day=1)
            fim = (ini.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        elif tipo == "Anual":
            ini, fim = d.replace(month=1, day=1), d.replace(month=12, day=31)
        else:
            ini = fim = d
        return ini, fim

    def _definir_periodo(self, tipo, ref=None):
        self._tipo_periodo = tipo
        self.seg_periodo.set(tipo)
        if tipo != "Personalizado":
            self._ref_periodo = ref or self._ref_periodo
            ini, fim = self._limites_periodo(tipo, self._ref_periodo)
            for ent, data in ((self.ent_rel_ini, ini), (self.ent_rel_fim, fim)):
                ent.delete(0, "end"); ent.insert(0, data.strftime("%d/%m/%Y"))
        self._atualizar_desc_periodo()
        self.atualizar_resumo_periodo()

    def _navegar_periodo(self, passo):
        per = self._ler_periodo(avisar=True)
        if not per:
            return
        ini, fim = per
        tipo = self._tipo_periodo
        if tipo == "Diário":
            ref = ini + timedelta(days=passo)
        elif tipo == "Semanal":
            ref = ini + timedelta(days=7 * passo)
        elif tipo == "Mensal":
            m = ini.month - 1 + passo
            ref = ini.replace(year=ini.year + m // 12, month=m % 12 + 1, day=1)
        elif tipo == "Anual":
            ref = ini.replace(year=ini.year + passo)
        else:
            # Personalizado: anda o mesmo número de dias do intervalo escolhido
            dias = (fim.date() - ini.date()).days + 1
            for ent, data in ((self.ent_rel_ini, ini), (self.ent_rel_fim, fim)):
                ent.delete(0, "end"); ent.insert(0, (data + timedelta(days=dias * passo)).strftime("%d/%m/%Y"))
            self._atualizar_desc_periodo(); self.atualizar_resumo_periodo()
            return
        self._definir_periodo(tipo, ref)

    def _periodo_ir_para_hoje(self):
        tipo = "Diário" if self._tipo_periodo == "Personalizado" else self._tipo_periodo
        self._definir_periodo(tipo, datetime.now())

    def _datas_digitadas(self, event=None):
        if event is not None and event.keysym in ("Tab", "Shift_L", "Shift_R", "Left", "Right", "Home", "End"):
            return
        if self._tipo_periodo != "Personalizado":
            self._tipo_periodo = "Personalizado"
            self.seg_periodo.set("Personalizado")
        self._agendar_refresh_lista("periodo", lambda: (self._atualizar_desc_periodo(), self.atualizar_resumo_periodo()), delay=400)

    @staticmethod
    def _parse_data(texto):
        """Aceita 05/11/2026, 5/11/26, 05-11-2026, 05112026."""
        t = (texto or "").strip().replace("-", "/").replace(".", "/")
        if t.isdigit() and len(t) in (6, 8):
            t = f"{t[:2]}/{t[2:4]}/{t[4:]}"
        for fmt in ("%d/%m/%Y", "%d/%m/%y"):
            try:
                return datetime.strptime(t, fmt)
            except ValueError:
                continue
        return None

    def _ler_periodo(self, avisar=False):
        ini = self._parse_data(self.ent_rel_ini.get())
        fim = self._parse_data(self.ent_rel_fim.get())
        if not ini or not fim:
            if avisar:
                messagebox.showwarning("Período", "Digite as datas de início e fim no formato DD/MM/AAAA.")
            return None
        if ini > fim:
            if avisar:
                messagebox.showwarning("Período", "A data de início é depois da data de fim.")
            return None
        return ini.replace(hour=0, minute=0, second=0), fim.replace(hour=23, minute=59, second=59)

    def _titulo_periodo(self, ini, fim):
        tipo = self._tipo_periodo
        if tipo == "Diário":
            return f"Diário - {ini:%d/%m/%Y}"
        if tipo == "Semanal":
            return f"Semanal - {ini:%d/%m} a {fim:%d/%m/%Y}"
        if tipo == "Mensal":
            return f"Mensal - {self.MESES[ini.month - 1]} de {ini.year}"
        if tipo == "Anual":
            return f"Anual - {ini.year}"
        if ini.date() == fim.date():
            return f"do Dia {ini:%d/%m/%Y}"
        return f"do Período {ini:%d/%m/%Y} a {fim:%d/%m/%Y}"

    def _atualizar_desc_periodo(self):
        per = self._ler_periodo()
        if not per:
            return self.lbl_desc_periodo.configure(text="⚠ datas inválidas", text_color="#ff5555")
        ini, fim = per
        dias = (fim.date() - ini.date()).days + 1
        self.lbl_desc_periodo.configure(text=f"{self._titulo_periodo(ini, fim)}  ({dias} dia{'s' if dias > 1 else ''})",
                                        text_color="#9575CD")

    def _periodo_anterior(self, ini, fim):
        """Período usado na comparação ▲▼ dos indicadores do painel."""
        tipo = self._tipo_periodo
        if tipo == "Diário":
            a = ini - timedelta(days=1)
            return a, a.replace(hour=23, minute=59, second=59), "dia anterior"
        if tipo == "Semanal":
            return ini - timedelta(days=7), fim - timedelta(days=7), "semana anterior"
        if tipo == "Mensal":
            a_ini, a_fim = self._limites_periodo("Mensal", ini - timedelta(days=1))
            return a_ini, a_fim.replace(hour=23, minute=59, second=59), "mês anterior"
        if tipo == "Anual":
            return ini.replace(year=ini.year - 1), fim.replace(year=fim.year - 1), "ano anterior"
        dias = (fim.date() - ini.date()).days + 1
        return ini - timedelta(days=dias), fim - timedelta(days=dias), "período anterior"

    def atualizar_painel(self):
        """Recalcula o painel para o período escolhido (roda na thread do banco)."""
        per = self._ler_periodo()
        if not per:
            return
        ini, fim = per
        ini_ant, fim_ant, rotulo_ant = self._periodo_anterior(ini, fim)
        dias = (fim.date() - ini.date()).days + 1
        granularidade = "hora" if dias == 1 else ("dia" if dias <= 62 else "mes")
        seq = self._novo_seq("painel")

        def mostrar(d):
            if seq == self._seq_listas.get("painel"):   # ignora respostas de um período antigo
                self.painel.mostrar(d, rotulo_ant)

        self.banco.enviar(bd.dados_painel, ini, fim, ini_ant, fim_ant, granularidade, ok=mostrar)

    # o restante do sistema chama este nome quando uma venda/entrada muda os números
    atualizar_resumo_periodo = atualizar_painel

    def gerar_relatorio_pdf(self):
        per = self._ler_periodo(avisar=True)
        if per:
            self._gerar_pdf_periodo(self._titulo_periodo(*per), *per)

    def imprimir_relatorio_periodo(self):
        per = self._ler_periodo(avisar=True)
        if not per:
            return
        titulo = self._titulo_periodo(*per)
        self.banco.enviar(bd.dados_periodo, *per, ok=lambda d: self.imprimir(self._texto_fechamento(d, titulo, *per)))

    # --- PDF (números vêm da thread do banco; o PDF é montado em outra thread) ---
    def _gerar_pdf_periodo(self, titulo_periodo, data_inicio, data_fim):
        if not TEM_REPORTLAB:
            return messagebox.showerror("Erro", "Instale a biblioteca: pip install reportlab")

        nome_sugerido = f"Relatorio_{normalizar_texto(titulo_periodo).title()}".replace(" ", "_").replace("/", "-")
        for ch in '<>:"\\|?*()':
            nome_sugerido = nome_sugerido.replace(ch, "")
        filename = filedialog.asksaveasfilename(defaultextension=".pdf", filetypes=[("PDF files", "*.pdf")],
                                                initialfile=f"{nome_sugerido}.pdf")
        if not filename:
            return

        self.notificar("📄 Gerando relatório...", "info", segundos=30)

        def com_dados(dados):
            self._em_segundo_plano(self._construir_pdf_periodo, titulo_periodo, data_inicio, data_fim, dados, filename,
                                   ok=self._pdf_pronto, erro_titulo="Erro PDF")

        self.banco.enviar(bd.dados_periodo, data_inicio, data_fim, ok=com_dados)

    def _pdf_pronto(self, filename):
        self.notificar(f"📄 Relatório salvo: {filename}", "ok")
        try:
            os.startfile(filename)
        except Exception:
            pass

    def _construir_pdf_periodo(self, titulo, ini, fim, dados, filename):
        styles = getSampleStyleSheet()
        doc = SimpleDocTemplate(filename, pagesize=A4, topMargin=36, bottomMargin=36, title=f"Relatório {titulo}")
        el = []
        varios_dias = (fim.date() - ini.date()).days >= 1
        dinheiro = moeda
        total = dados["total_geral"]

        def secao(texto):
            el.append(Paragraph(f"<b>{texto}</b>", styles['Heading2']))
            el.append(Spacer(1, 4))

        def tabela(linhas, larguras, cor, com_total=False):
            t = Table(linhas, colWidths=larguras, repeatRows=1)
            estilo = [
                ('BACKGROUND', (0, 0), (-1, 0), cor),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f2f2")]),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]
            if com_total:
                estilo += [('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                           ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor("#e0e0e0"))]
            t.setStyle(TableStyle(estilo))
            el.append(t)
            el.append(Spacer(1, 14))

        el.append(Paragraph(f"Relatório {titulo}", styles['Title']))
        el.append(Paragraph("Sistema Dois Irmãos — Água e Gás", styles['Normal']))
        el.append(Paragraph(f"Período: {ini:%d/%m/%Y} a {fim:%d/%m/%Y} &nbsp;•&nbsp; Gerado em {datetime.now():%d/%m/%Y %H:%M}",
                            styles['Normal']))
        el.append(Spacer(1, 14))

        # 1. RESUMO
        secao("RESUMO")
        unidades = sum(q for _, q, _ in dados["produtos_vendidos"])
        resumo = [["Indicador", "Valor"],
                  ["Quantidade de vendas", str(dados["qtd_vendas"])],
                  ["Faturamento total", dinheiro(total)],
                  ["Ticket médio (por venda)", dinheiro(dados["ticket_medio"])],
                  ["Taxas de entrega (já incluídas no total)", dinheiro(dados["total_taxas"])],
                  ["Itens vendidos (unidades)", self._formatar_qtd(unidades)]]
        if varios_dias:
            dias_venda = len(dados["vendas_por_dia"])
            resumo.append(["Dias com venda", str(dias_venda)])
            resumo.append(["Média por dia com venda", dinheiro(total / dias_venda) if dias_venda else "-"])
        tabela(resumo, [320, 180], colors.HexColor("#424242"))

        # 2. FORMAS DE PAGAMENTO
        secao("FATURAMENTO POR FORMA DE PAGAMENTO")
        fin = [["Forma de pagamento", "Vendas", "Valor", "% do total"]]
        for forma, info in dados["financeiro_por_forma"].items():
            pct = (info["valor"] / total * 100) if total else 0
            fin.append([forma, str(info["qtd"]), dinheiro(info["valor"]), f"{pct:.1f}%".replace(".", ",")])
        if len(fin) == 1:
            fin.append(["(nenhuma venda no período)", "-", "-", "-"])
        fin.append(["TOTAL", str(dados["qtd_vendas"]), dinheiro(total), "100%" if total else "-"])
        tabela(fin, [200, 80, 130, 90], colors.grey, com_total=True)

        # 3. DIA A DIA (só quando o período tem mais de um dia)
        if varios_dias:
            secao("VENDAS DIA A DIA")
            pd = [["Data", "Vendas", "Valor"]] + [[d, str(q), dinheiro(v)] for d, q, v in dados["vendas_por_dia"]]
            if len(pd) == 1:
                pd.append(["(nenhuma venda no período)", "-", "-"])
            pd.append(["TOTAL", str(dados["qtd_vendas"]), dinheiro(total)])
            tabela(pd, [220, 120, 160], colors.HexColor("#3F51B5"), com_total=True)

        # 4. PRODUTOS VENDIDOS — mais vendidos primeiro
        secao("PRODUTOS VENDIDOS (SAÍDAS)")
        prods = sorted(dados["produtos_vendidos"], key=lambda r: (-r[1], r[0]))
        ps = [["Produto", "Quantidade", "Valor vendido"]] + [[n, self._formatar_qtd(q), dinheiro(v)] for n, q, v in prods]
        if len(ps) == 1:
            ps.append(["(nenhuma venda no período)", "-", "-"])
        ps.append(["TOTAL (sem taxas de entrega)", self._formatar_qtd(unidades), dinheiro(sum(v for _, _, v in prods))])
        tabela(ps, [290, 90, 120], colors.darkblue, com_total=True)

        # 5. ENTRADAS DE MERCADORIA
        secao(f"ENTRADAS DE MERCADORIA — {dados['qtd_entradas']} registro(s) no período")
        en = [["Produto", "Quantidade recebida"]] + [[n, self._formatar_qtd(q)] for n, q in dados["produtos_entrada"]]
        if len(en) == 1:
            en.append(["(nenhuma entrada no período)", "-"])
        else:
            en.append(["TOTAL", self._formatar_qtd(sum(q for _, q in dados["produtos_entrada"]))])
        tabela(en, [370, 130], colors.HexColor("#FF9800"), com_total=len(en) > 2)

        try:
            doc.build(el)
        except PermissionError:
            raise RuntimeError("Não foi possível salvar: o arquivo está aberto em outro programa?\nFeche o PDF e tente de novo.")
        return filename

    # --- Resumo para a impressora térmica (48 colunas, sem acentos) ---
    def _texto_fechamento(self, dados, titulo, ini, fim):
        L = 48
        linha, traco = "=" * L, "-" * L
        sa = normalizar_texto  # a impressora térmica não imprime acentos direito
        varios_dias = (fim.date() - ini.date()).days >= 1

        txt = "\n" + "DOIS IRMAOS - AGUA E GAS".center(L) + "\n"
        txt += sa(f"RELATORIO {titulo}")[:L].center(L) + "\n"
        txt += f"{ini:%d/%m/%Y} A {fim:%d/%m/%Y}".center(L) + "\n"
        txt += f"Emitido em {datetime.now():%d/%m/%Y %H:%M}".center(L) + "\n" + linha + "\n"

        txt += f"{'VENDAS:':<20}{dados['qtd_vendas']:>28}\n"
        txt += f"{'FATURAMENTO:':<20}{moeda(dados['total_geral']):>28}\n"
        txt += f"{'TICKET MEDIO:':<20}{moeda(dados['ticket_medio']):>28}\n"
        txt += f"{'TAXAS ENTREGA:':<20}{moeda(dados['total_taxas']):>28}\n" + linha + "\n"

        txt += ">>> FORMAS DE PAGAMENTO\n" + f"{'FORMA':<20} {'QTD':^6} {'VALOR':>20}\n" + traco + "\n"
        for forma, info in dados["financeiro_por_forma"].items():
            txt += f"{sa(forma)[:20]:<20} {info['qtd']:^6} {moeda(info['valor']):>20}\n"
        txt += linha + "\n"

        if varios_dias:
            txt += ">>> VENDAS DIA A DIA\n" + f"{'DATA':<20} {'QTD':^6} {'VALOR':>20}\n" + traco + "\n"
            for d, q, v in dados["vendas_por_dia"]:
                txt += f"{d:<20} {q:^6} {moeda(v):>20}\n"
            txt += linha + "\n"

        txt += ">>> PRODUTOS VENDIDOS (SAIDAS)\n" + f"{'PRODUTO':<26} {'QTD':^8} {'VALOR':>12}\n" + traco + "\n"
        total_itens = 0.0
        for nome, qtd, valor in sorted(dados["produtos_vendidos"], key=lambda r: (-r[1], r[0])):
            txt += f"{sa(nome)[:26]:<26} {self._formatar_qtd(qtd):^8} {moeda(valor, False):>12}\n"
            total_itens += qtd
        txt += traco + "\n" + f"{'TOTAL DE ITENS:':<26} {self._formatar_qtd(total_itens):^8}\n" + linha + "\n"

        if dados["produtos_entrada"]:
            txt += ">>> ENTRADAS DE MERCADORIA\n" + traco + "\n"
            for nome, qtd in dados["produtos_entrada"]:
                txt += f"{sa(nome)[:36]:<36} {self._formatar_qtd(qtd):>11}\n"
            txt += linha + "\n"
        return txt + "\n\n"

    # =========================================================================
    # LAYOUTS
    # =========================================================================
    def montar_aba_venda(self):
        self.tab_venda.grid_columnconfigure(0, weight=1); self.tab_venda.grid_columnconfigure(1, weight=1); self.tab_venda.grid_rowconfigure(0, weight=1)

        # ESQUERDA
        self.f_esq = ctk.CTkFrame(self.tab_venda)
        self.f_esq.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")

        ctk.CTkButton(self.f_esq, text="📱 IP CELULAR", fg_color="#673AB7", command=self.mostrar_info_celular).pack(fill="x", padx=10, pady=5)
        self.btn_backup = ctk.CTkButton(self.f_esq, text="💾 SALVAR INFORMAÇÕES", fg_color="#009688", command=self.salvar_backup_manual)
        self.btn_backup.pack(fill="x", padx=10, pady=5)
        self.f_drive = f_drive = ctk.CTkFrame(self.f_esq, fg_color="transparent")
        f_drive.pack(fill="x", padx=10, pady=5)
        self.btn_drive = ctk.CTkButton(f_drive, text="☁️ BACKUP NO DRIVE", fg_color="#1565C0", command=self.backup_externo_agora)
        self.btn_drive.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(f_drive, text="⚙", width=36, fg_color="#37474F", hover_color="#263238",
                      command=self.escolher_pasta_backup_externo).pack(side="left", padx=(5, 0))

        # Faixa "modo edição" (fica escondida até editar uma venda antiga)
        self.f_modo_edicao = ctk.CTkFrame(self.f_esq, fg_color="red")
        self.lbl_modo_edicao = ctk.CTkLabel(self.f_modo_edicao, text="MODO EDIÇÃO", font=("Arial", 12, "bold"))
        self.lbl_modo_edicao.pack(side="left", fill="x", expand=True, padx=5)
        ctk.CTkButton(self.f_modo_edicao, text="✖ CANCELAR EDIÇÃO", width=140, fg_color="#7a0000", hover_color="#550000",
                      command=self.cancelar_edicao_venda).pack(side="right", padx=5, pady=3)

        ctk.CTkLabel(self.f_esq, text="CLIENTE", font=("Arial", 12, "bold")).pack(anchor="w", padx=10, pady=(5, 0))
        self.ent_cli = ctk.CTkEntry(self.f_esq, placeholder_text="Nome..."); self.ent_cli.pack(fill="x", padx=10, pady=5)
        self.ent_end = ctk.CTkEntry(self.f_esq, placeholder_text="Endereço..."); self.ent_end.pack(fill="x", padx=10, pady=5)

        ctk.CTkLabel(self.f_esq, text="PRODUTO", font=("Arial", 12, "bold")).pack(anchor="w", padx=10, pady=(10, 0))
        self.ent_prod_venda = ctk.CTkEntry(self.f_esq, placeholder_text="Buscar... (Enter escolhe, ↓↑ navega)")
        self.ent_prod_venda.pack(fill="x", padx=10, pady=5)

        f_qtd = ctk.CTkFrame(self.f_esq, fg_color="transparent"); f_qtd.pack(fill="x", padx=10, pady=5)
        self.ent_qtd = ctk.CTkEntry(f_qtd, width=60); self.ent_qtd.pack(side="left"); self.ent_qtd.insert(0, "1")
        self.ent_qtd.bind("<Return>", lambda e: self.add_item())
        self.ent_qtd.bind("<KP_Enter>", lambda e: self.add_item())
        ctk.CTkButton(f_qtd, text="+ ADICIONAR", width=120, fg_color="green", command=self.add_item).pack(side="right", fill="x", expand=True, padx=(10, 0))

        def ir_para_qtd(_=None):
            self.ent_qtd.focus_set(); self.ent_qtd.select_range(0, "end")

        def cliente_escolhido(_=None):
            self.buscar_endereco_cliente(); self.ent_prod_venda.focus_set()

        Autocomplete(self, self.ent_cli, "clientes", ao_escolher=cliente_escolhido, ao_enter=cliente_escolhido)
        Autocomplete(self, self.ent_prod_venda, "prods", ao_escolher=ir_para_qtd, ao_enter=self.add_item)
        self.ent_cli.bind("<FocusOut>", lambda e: (not self.ent_end.get().strip()) and self.buscar_endereco_cliente())

        ctk.CTkLabel(self.f_esq, text="PAGAMENTO", font=("Arial", 12, "bold")).pack(anchor="w", padx=10, pady=(15, 0))
        self.forma_pagamento = ctk.CTkOptionMenu(self.f_esq, values=["Dinheiro", "Cartão", "PIX", "Link", "Outros"], command=lambda e: self.atualizar_totais())
        self.forma_pagamento.pack(fill="x", padx=10, pady=5)
        self.ent_taxa_entrega = ctk.CTkEntry(self.f_esq, placeholder_text="Taxa Entrega"); self.ent_taxa_entrega.pack(fill="x", padx=10, pady=5)
        self.ent_valor_pago = ctk.CTkEntry(self.f_esq, placeholder_text="Valor Recebido"); self.ent_valor_pago.pack(fill="x", padx=10, pady=5)

        # DIREITA
        self.f_dir = ctk.CTkFrame(self.tab_venda)
        self.f_dir.grid(row=0, column=1, padx=10, pady=10, sticky="nsew")
        ctk.CTkLabel(self.f_dir, text="ITENS DA VENDA (duplo clique na quantidade para editar • Delete remove)", text_color="gray").pack(pady=(5, 0))

        f_carrinho = ctk.CTkFrame(self.f_dir, fg_color="transparent")
        f_carrinho.pack(fill="both", expand=True, padx=5, pady=5)
        self.cols_carrinho = ("ITEM", "QTD", "UNIT", "TOTAL")
        self.tree_carrinho = ttk.Treeview(f_carrinho, columns=self.cols_carrinho, show="headings", selectmode="browse")
        self.tree_carrinho.heading("ITEM", text="PRODUTO"); self.tree_carrinho.column("ITEM", width=200, anchor="w")
        self.tree_carrinho.heading("QTD", text="QTD"); self.tree_carrinho.column("QTD", width=50, anchor="center")
        self.tree_carrinho.heading("UNIT", text="UNIT"); self.tree_carrinho.column("UNIT", width=80, anchor="e")
        self.tree_carrinho.heading("TOTAL", text="TOTAL"); self.tree_carrinho.column("TOTAL", width=80, anchor="e")
        self.tree_carrinho.tag_configure("baixo", foreground="#FFC107")
        vsb_car = ttk.Scrollbar(f_carrinho, orient="vertical", command=self.tree_carrinho.yview)
        self.tree_carrinho.configure(yscrollcommand=vsb_car.set)
        self.tree_carrinho.pack(side="left", fill="both", expand=True)
        vsb_car.pack(side="right", fill="y")
        self.tree_carrinho.bind("<Double-1>", self.editar_item_carrinho)
        self.tree_carrinho.bind("<Return>", self.editar_item_carrinho)
        self.tree_carrinho.bind("<Delete>", self.excluir_item_carrinho)

        f_tot = ctk.CTkFrame(self.f_dir); f_tot.pack(fill="x", pady=5)
        self.lbl_troco = ctk.CTkLabel(f_tot, text="TROCO: --", font=("Arial", 14, "bold"), text_color="green"); self.lbl_troco.pack(side="left", padx=10)
        self.lbl_total = ctk.CTkLabel(f_tot, text="TOTAL: R$ 0.00", font=("Arial", 24, "bold"), text_color="yellow"); self.lbl_total.pack(side="right", padx=10)

        f_botoes = ctk.CTkFrame(self.f_dir, fg_color="transparent")
        f_botoes.pack(fill="x", padx=10, pady=5)
        self.btn_salvar_apenas = ctk.CTkButton(f_botoes, text="SALVAR (F4)", height=50, fg_color="#4CAF50", command=lambda: self.finalizar_venda(imprimir=False))
        self.btn_salvar_apenas.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.btn_finalizar = ctk.CTkButton(f_botoes, text="IMPRIMIR (F5)", height=50, fg_color="#2196F3", command=lambda: self.finalizar_venda(imprimir=True))
        self.btn_finalizar.pack(side="right", fill="x", expand=True, padx=(5, 0))

        self.ent_taxa_entrega.bind("<KeyRelease>", lambda e: self.atualizar_totais())
        self.ent_valor_pago.bind("<KeyRelease>", lambda e: self.atualizar_totais())
        self.bind("<F5>", lambda e: self.finalizar_venda(imprimir=True))
        self.bind("<F4>", lambda e: self.finalizar_venda(imprimir=False))

    def montar_aba_clientes(self):
        f = ctk.CTkFrame(self.tab_clientes); f.pack(fill="x", padx=10, pady=10)
        self.ent_c_nome = ctk.CTkEntry(f, placeholder_text="Nome"); self.ent_c_nome.pack(side="left", expand=True, fill="x", padx=5)
        self.ent_c_end = ctk.CTkEntry(f, placeholder_text="Endereço"); self.ent_c_end.pack(side="left", expand=True, fill="x", padx=5)
        self.ent_c_end.bind("<Return>", lambda e: self.salvar_cliente())
        self.btn_salvar_cli = ctk.CTkButton(f, text="SALVAR", fg_color="green", width=100, command=self.salvar_cliente)
        self.btn_salvar_cli.pack(side="left", padx=5)
        self.btn_cancelar_cli = ctk.CTkButton(f, text="X", width=30, fg_color="red", command=self.cancelar_edicao_cli); self.btn_cancelar_cli.pack(side="left", padx=2); self.btn_cancelar_cli.pack_forget()

        f_acoes = ctk.CTkFrame(self.tab_clientes, fg_color="transparent"); f_acoes.pack(fill="x", padx=10)
        ctk.CTkButton(f_acoes, text="EDITAR SELECIONADO", fg_color="#007bff", command=self.carregar_cliente_para_edicao).pack(side="left", padx=5)
        ctk.CTkButton(f_acoes, text="EXCLUIR SELECIONADO(S)", fg_color="red", command=self.excluir_clientes_selecionados).pack(side="left", padx=5)

        self.ent_busca_cli = ctk.CTkEntry(self.tab_clientes, placeholder_text="🔍 Buscar por nome ou endereço (ex.: joão, rua das flores)...")
        self.ent_busca_cli.pack(fill="x", padx=10, pady=5)
        self.ent_busca_cli.bind("<KeyRelease>", lambda e: self._agendar_refresh_lista("clientes", self._buscar_lista_clientes, delay=220))

        f_tree = ctk.CTkFrame(self.tab_clientes, fg_color="transparent")
        f_tree.pack(fill="both", expand=True, padx=10, pady=5)
        self.cols_cli = ("ID", "NOME", "ENDERECO")
        self.tree_cli = ttk.Treeview(f_tree, columns=self.cols_cli, show="headings", selectmode="extended")
        self.tree_cli.heading("ID", text="ID"); self.tree_cli.column("ID", width=50, anchor="center")
        self.tree_cli.heading("NOME", text="NOME"); self.tree_cli.column("NOME", width=400, anchor="w")
        self.tree_cli.heading("ENDERECO", text="ENDEREÇO"); self.tree_cli.column("ENDERECO", width=600, anchor="w")
        vsb = ttk.Scrollbar(f_tree, orient="vertical", command=self.tree_cli.yview)
        self.tree_cli.configure(yscrollcommand=vsb.set)
        self.tree_cli.pack(side="left", fill="both", expand=True); vsb.pack(side="right", fill="y")
        self.tree_cli.bind("<Double-1>", lambda e: self.carregar_cliente_para_edicao())

        self.atualizar_lista_clientes()

    def montar_aba_produtos(self):
        f_topo = ctk.CTkFrame(self.tab_produtos); f_topo.pack(fill="x", padx=10, pady=10)
        ctk.CTkLabel(f_topo, text="CADASTRO DE PRODUTO", font=("Arial", 12, "bold")).pack(anchor="w", padx=5)
        f_cad = ctk.CTkFrame(f_topo, fg_color="transparent"); f_cad.pack(fill="x", pady=2)
        self.ent_p_nome = ctk.CTkEntry(f_cad, placeholder_text="Nome Produto"); self.ent_p_nome.pack(side="left", expand=True, fill="x", padx=5)
        self.ent_p_preco = ctk.CTkEntry(f_cad, placeholder_text="Preço R$", width=100); self.ent_p_preco.pack(side="left", padx=5)
        self.ent_p_preco.bind("<Return>", lambda e: self.salvar_produto())
        self.btn_salvar_prod = ctk.CTkButton(f_cad, text="SALVAR / CRIAR", fg_color="green", width=120, command=self.salvar_produto); self.btn_salvar_prod.pack(side="left", padx=5)
        self.btn_cancelar_prod = ctk.CTkButton(f_cad, text="X", width=30, fg_color="red", command=self.cancelar_edicao_prod); self.btn_cancelar_prod.pack(side="left", padx=2); self.btn_cancelar_prod.pack_forget()

        f_busca = ctk.CTkFrame(self.tab_produtos); f_busca.pack(fill="x", padx=10, pady=5)
        self.ent_busca_prod = ctk.CTkEntry(f_busca, placeholder_text="🔍 Filtrar tabela..."); self.ent_busca_prod.pack(side="left", fill="x", expand=True, padx=5)
        self.ent_busca_prod.bind("<KeyRelease>", lambda e: self._agendar_refresh_lista("produtos", self._buscar_lista_produtos, delay=220))
        self.var_so_baixo = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(f_busca, text=f"Só estoque baixo (< {ESTOQUE_MINIMO})", variable=self.var_so_baixo,
                        command=self.atualizar_lista_produtos).pack(side="left", padx=10)
        ctk.CTkButton(f_busca, text="✏ EDITAR", width=100, fg_color="#007bff", command=self.carregar_produto_para_edicao).pack(side="left", padx=5)
        ctk.CTkButton(f_busca, text="🗑 EXCLUIR", width=100, fg_color="red", command=self.excluir_produtos_selecionados).pack(side="left", padx=5)
        ctk.CTkButton(f_busca, text="🚚 DAR ENTRADA", width=120, fg_color="#FF9800", text_color="black",
                      command=self._ir_para_entrada_do_selecionado).pack(side="left", padx=5)
        self.lbl_resumo_estoque = ctk.CTkLabel(self.tab_produtos, text="", text_color="gray", anchor="w")
        self.lbl_resumo_estoque.pack(fill="x", padx=15)

        f_tree = ctk.CTkFrame(self.tab_produtos, fg_color="transparent")
        f_tree.pack(fill="both", expand=True, padx=10, pady=5)
        self.cols_prod = ("ID", "NOME", "PRECO", "ESTOQUE", "SAIDAS")
        self.tree_prod = ttk.Treeview(f_tree, columns=self.cols_prod, show="headings", selectmode="extended")
        self.tree_prod.heading("ID", text="ID"); self.tree_prod.column("ID", width=50, anchor="center")
        self.tree_prod.heading("NOME", text="PRODUTO"); self.tree_prod.column("NOME", width=400, anchor="w")
        self.tree_prod.heading("PRECO", text="PREÇO"); self.tree_prod.column("PRECO", width=100, anchor="center")
        self.tree_prod.heading("ESTOQUE", text="ESTOQUE"); self.tree_prod.column("ESTOQUE", width=120, anchor="center")
        self.tree_prod.heading("SAIDAS", text="SAIDAS (HOJE)"); self.tree_prod.column("SAIDAS", width=120, anchor="center")
        self.tree_prod.tag_configure("baixo", foreground="#ff5555")
        self.tree_prod.tag_configure("normal", foreground="white")
        vsb = ttk.Scrollbar(f_tree, orient="vertical", command=self.tree_prod.yview)
        self.tree_prod.configure(yscrollcommand=vsb.set)
        self.tree_prod.pack(side="left", fill="both", expand=True); vsb.pack(side="right", fill="y")
        self.tree_prod.bind("<Double-1>", lambda e: self.carregar_produto_para_edicao())

        self.atualizar_lista_produtos()

    def montar_aba_entrada(self):
        f_entrada = ctk.CTkFrame(self.tab_entrada, border_color="#FF9800", border_width=2); f_entrada.pack(fill="x", padx=10, pady=10)
        ctk.CTkLabel(f_entrada, text="🚚 CHEGADA DE MERCADORIA", font=("Arial", 14, "bold"), text_color="#FF9800").pack(anchor="w", padx=10, pady=(8, 2))
        ctk.CTkLabel(f_entrada, text="Digite o produto (Enter escolhe), a quantidade que chegou e Enter para registrar.",
                     text_color="gray").pack(anchor="w", padx=10)
        f_ent_cont = ctk.CTkFrame(f_entrada, fg_color="transparent"); f_ent_cont.pack(fill="x", padx=5, pady=8)
        self.ent_entrada_busca = ctk.CTkEntry(f_ent_cont, placeholder_text="Busque produto...", height=36)
        self.ent_entrada_busca.pack(side="left", fill="x", expand=True, padx=5)
        self.ent_entrada_qtd = ctk.CTkEntry(f_ent_cont, placeholder_text="Qtd Chegou", width=110, height=36)
        self.ent_entrada_qtd.pack(side="left", padx=5)
        self.ent_entrada_qtd.bind("<Return>", lambda e: self.dar_entrada_estoque())
        self.ent_entrada_qtd.bind("<KP_Enter>", lambda e: self.dar_entrada_estoque())
        ctk.CTkButton(f_ent_cont, text="➕ REGISTRAR ENTRADA", height=36, fg_color="#FF9800", text_color="black",
                      command=self.dar_entrada_estoque).pack(side="left", padx=5)
        self.lbl_estoque_entrada = ctk.CTkLabel(f_entrada, text="Estoque atual: —", font=("Arial", 13, "bold"), anchor="w")
        self.lbl_estoque_entrada.pack(fill="x", padx=12, pady=(0, 8))

        def produto_escolhido(_=None):
            self._mostrar_estoque_entrada()
            self.ent_entrada_qtd.focus_set()

        Autocomplete(self, self.ent_entrada_busca, "prods", ao_escolher=produto_escolhido, ao_enter=produto_escolhido)

        # --- Histórico de entradas ---
        f_hist = ctk.CTkFrame(self.tab_entrada); f_hist.pack(fill="x", padx=10, pady=(5, 0))
        ctk.CTkLabel(f_hist, text="HISTÓRICO DE ENTRADAS", font=("Arial", 12, "bold")).pack(side="left", padx=10)
        refresh = lambda e: self._agendar_refresh_lista("entradas", self._buscar_lista_entradas, delay=220)
        self.ent_busca_entrada = ctk.CTkEntry(f_hist, placeholder_text="🔍 Produto...", width=220); self.ent_busca_entrada.pack(side="left", padx=5, pady=5)
        self.ent_busca_entrada.bind("<KeyRelease>", refresh)
        self.ent_data_entrada = ctk.CTkEntry(f_hist, placeholder_text="Data (DD/MM/AAAA)", width=140); self.ent_data_entrada.pack(side="left", padx=5)
        self.ent_data_entrada.bind("<KeyRelease>", refresh)
        ctk.CTkButton(f_hist, text="🗑 EXCLUIR ENTRADA(S)", fg_color="red", width=160,
                      command=self.excluir_entradas_selecionadas).pack(side="right", padx=5)

        f_tree = ctk.CTkFrame(self.tab_entrada, fg_color="transparent")
        f_tree.pack(fill="both", expand=True, padx=10, pady=5)
        self.tree_ent = ttk.Treeview(f_tree, columns=("ID", "DATA", "PRODUTO", "QTD"), show="headings", selectmode="extended")
        self.tree_ent.heading("ID", text="ID"); self.tree_ent.column("ID", width=60, anchor="center")
        self.tree_ent.heading("DATA", text="DATA/HORA"); self.tree_ent.column("DATA", width=150, anchor="center")
        self.tree_ent.heading("PRODUTO", text="PRODUTO"); self.tree_ent.column("PRODUTO", width=500, anchor="w")
        self.tree_ent.heading("QTD", text="QTD RECEBIDA"); self.tree_ent.column("QTD", width=120, anchor="center")
        vsb = ttk.Scrollbar(f_tree, orient="vertical", command=self.tree_ent.yview)
        self.tree_ent.configure(yscrollcommand=vsb.set)
        self.tree_ent.pack(side="left", fill="both", expand=True); vsb.pack(side="right", fill="y")

        self.atualizar_lista_entradas()

    def montar_aba_historico(self):
        """Histórico de vendas: consultar, reimprimir, editar e excluir."""
        f = ctk.CTkFrame(self.tab_historico); f.pack(fill="x", padx=10, pady=10)
        refresh = lambda e: self._agendar_refresh_lista("vendas", self._buscar_lista_vendas, delay=220)
        ctk.CTkLabel(f, text="🔍 Filtrar:", font=("Arial", 12, "bold")).pack(side="left", padx=(10, 5))
        self.ent_id_rel = ctk.CTkEntry(f, placeholder_text="Nº venda", width=80); self.ent_id_rel.pack(side="left", padx=5, pady=8)
        self.ent_id_rel.bind("<KeyRelease>", refresh)
        self.ent_data_rel = ctk.CTkEntry(f, placeholder_text="Data (DD/MM/AAAA)", width=140); self.ent_data_rel.pack(side="left", padx=5)
        self.ent_data_rel.bind("<KeyRelease>", refresh)
        self.ent_cli_rel = ctk.CTkEntry(f, placeholder_text="Nome do cliente...", width=200); self.ent_cli_rel.pack(side="left", fill="x", expand=True, padx=5)
        self.ent_cli_rel.bind("<KeyRelease>", refresh)

        f_acoes = ctk.CTkFrame(self.tab_historico, fg_color="transparent"); f_acoes.pack(fill="x", padx=10)
        ctk.CTkButton(f_acoes, text="🖨 REIMPRIMIR CUPOM", fg_color="grey", command=self.reimprimir_cupom).pack(side="left", padx=5)
        ctk.CTkButton(f_acoes, text="✏ EDITAR VENDA", fg_color="purple", command=self.editar_venda_antiga).pack(side="left", padx=5)
        ctk.CTkButton(f_acoes, text="🗑 EXCLUIR", fg_color="red", command=self.excluir_vendas_selecionadas).pack(side="left", padx=5)
        ctk.CTkLabel(f_acoes, text="Duplo clique numa venda para editar.", text_color="gray").pack(side="right", padx=10)

        f_tree = ctk.CTkFrame(self.tab_historico, fg_color="transparent")
        f_tree.pack(fill="both", expand=True, padx=10, pady=8)
        self.cols_rel = ("ID", "DATA", "CLIENTE", "TOTAL", "FORMA")
        self.tree_rel = ttk.Treeview(f_tree, columns=self.cols_rel, show="headings", selectmode="extended")
        self.tree_rel.heading("ID", text="Nº"); self.tree_rel.column("ID", width=60, anchor="center")
        self.tree_rel.heading("DATA", text="DATA/HORA"); self.tree_rel.column("DATA", width=150, anchor="center")
        self.tree_rel.heading("CLIENTE", text="CLIENTE"); self.tree_rel.column("CLIENTE", width=400, anchor="w")
        self.tree_rel.heading("TOTAL", text="TOTAL R$"); self.tree_rel.column("TOTAL", width=100, anchor="e")
        self.tree_rel.heading("FORMA", text="PAGAMENTO"); self.tree_rel.column("FORMA", width=150, anchor="center")
        vsb = ttk.Scrollbar(f_tree, orient="vertical", command=self.tree_rel.yview)
        self.tree_rel.configure(yscrollcommand=vsb.set)
        self.tree_rel.pack(side="left", fill="both", expand=True); vsb.pack(side="right", fill="y")
        self.tree_rel.bind("<Double-1>", lambda e: self.editar_venda_antiga())

        self.atualizar_lista_vendas()

    def montar_aba_relatorio(self):
        """Seletor de período + painel (dashboard) que muda junto com o período."""
        f_periodo = ctk.CTkFrame(self.tab_relatorio, border_color="#673AB7", border_width=2)
        f_periodo.pack(fill="x", padx=10, pady=(8, 6))

        f_tipo = ctk.CTkFrame(f_periodo, fg_color="transparent"); f_tipo.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(f_tipo, text="📊 Período:", font=("Arial", 13, "bold"), text_color="#9575CD").pack(side="left")
        self.seg_periodo = ctk.CTkSegmentedButton(f_tipo, values=self.TIPOS_PERIODO,
                                                  command=lambda tipo: self._definir_periodo(tipo))
        self.seg_periodo.pack(side="left", padx=10)
        ctk.CTkButton(f_tipo, text="🖨 IMPRIMIR RESUMO", fg_color="orange", text_color="black", width=150,
                      command=self.imprimir_relatorio_periodo).pack(side="right", padx=5)
        ctk.CTkButton(f_tipo, text="📄 GERAR PDF", fg_color="#673AB7", width=130,
                      command=self.gerar_relatorio_pdf).pack(side="right", padx=5)

        f_datas = ctk.CTkFrame(f_periodo, fg_color="transparent"); f_datas.pack(fill="x", padx=10, pady=(2, 8))
        ctk.CTkLabel(f_datas, text="De:").pack(side="left")
        self.ent_rel_ini = ctk.CTkEntry(f_datas, width=110, placeholder_text="DD/MM/AAAA"); self.ent_rel_ini.pack(side="left", padx=5)
        ctk.CTkLabel(f_datas, text="Até:").pack(side="left", padx=(8, 0))
        self.ent_rel_fim = ctk.CTkEntry(f_datas, width=110, placeholder_text="DD/MM/AAAA"); self.ent_rel_fim.pack(side="left", padx=5)
        for ent in (self.ent_rel_ini, self.ent_rel_fim):
            ent.bind("<KeyRelease>", self._datas_digitadas)
        ctk.CTkButton(f_datas, text="◀", width=36, command=lambda: self._navegar_periodo(-1)).pack(side="left", padx=(15, 2))
        ctk.CTkButton(f_datas, text="HOJE", width=60, command=self._periodo_ir_para_hoje).pack(side="left", padx=2)
        ctk.CTkButton(f_datas, text="▶", width=36, command=lambda: self._navegar_periodo(+1)).pack(side="left", padx=2)
        self.lbl_desc_periodo = ctk.CTkLabel(f_datas, text="", text_color="#9575CD", font=("Arial", 13, "bold"))
        self.lbl_desc_periodo.pack(side="left", padx=15)

        self.painel = Painel(self.tab_relatorio)
        self.painel.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        self._tipo_periodo = "Diário"
        self._ref_periodo = datetime.now()
        self.seg_periodo.set("Diário")
        self._definir_periodo("Diário")

    # =========================================================================
    # CARRINHO
    # =========================================================================
    def renderizar_carrinho(self):
        self.tree_carrinho.delete(*self.tree_carrinho.get_children())
        self.total_itens = 0.0
        for i, item in enumerate(self.carrinho):
            if 'vu' not in item:
                item['vu'] = item['t'] / item['q'] if item['q'] else 0
            tags = ("baixo",) if item.get("_estoque_baixo") else ()
            self.tree_carrinho.insert("", "end", iid=str(i), tags=tags,
                                      values=(item['n'], self._formatar_qtd(item['q']), f"R$ {item['vu']:.2f}", f"R$ {item['t']:.2f}"))
            self.total_itens += item['t']
        self.atualizar_totais()

    def add_item(self):
        nome_digitado = self.ent_prod_venda.get()
        if not normalizar_texto(nome_digitado):
            return
        txt_qtd = self.ent_qtd.get().strip()
        q = 1.0 if not txt_qtd else self.safe_float(txt_qtd)
        if q <= 0:
            self.notificar("Digite uma quantidade maior que zero.", "aviso")
            self.ent_qtd.focus_set(); self.ent_qtd.select_range(0, "end")
            return
        # A busca vai para a thread do banco; o resultado volta em _adicionar_ao_carrinho
        self.banco.enviar(bd.buscar_produto, nome_digitado, ok=lambda res: self._adicionar_ao_carrinho(res, q, nome_digitado))

    def _adicionar_ao_carrinho(self, res, q, nome_digitado):
        if not res:
            self.notificar(f"Produto '{nome_digitado.strip()}' não encontrado.", "erro")
            self.ent_prod_venda.focus_set(); self.ent_prod_venda.select_range(0, "end")
            return
        pid, nome_real, preco, estoque_atual = res
        preco = float(preco or 0)

        existente = next((i for i in self.carrinho if i.get('pid') == pid or
                          (not i.get('pid') and normalizar_texto(i.get('n')) == normalizar_texto(nome_real))), None)
        q_total = q + (self.safe_float(existente.get('q')) if existente else 0)

        if existente:
            existente.update(pid=pid, q=q_total)
            existente.setdefault('vu', preco)
            existente['t'] = q_total * existente['vu']
            item = existente
        else:
            item = {"pid": pid, "n": nome_real.upper(), "q": q, "vu": preco, "t": q * preco}
            self.carrinho.append(item)

        # Estoque baixo: AVISA sem travar (antes era uma janela que bloqueava tudo)
        item["_estoque_baixo"] = estoque_atual < q_total
        if item["_estoque_baixo"]:
            self.notificar(f"⚠ Estoque baixo: {nome_real} tem só {self._formatar_qtd(estoque_atual)} un (no carrinho: {self._formatar_qtd(q_total)}).", "aviso", 12)
        else:
            self.notificar(f"+ {self._formatar_qtd(q)} x {nome_real}", "ok", 4)
        self.renderizar_carrinho()

        if self.ent_prod_venda.get() == nome_digitado:
            self.ent_prod_venda.delete(0, 'end')
        self.ent_qtd.delete(0, 'end'); self.ent_qtd.insert(0, "1")
        self.ent_prod_venda.focus_set()

    def editar_item_carrinho(self, event=None):
        """Edita a quantidade DIRETO na linha do carrinho (sem janela)."""
        sel = self.tree_carrinho.selection()
        if not sel:
            return
        iid = sel[0]
        bbox = self.tree_carrinho.bbox(iid, "QTD")
        if not bbox:
            return
        if self._editor_carrinho is not None:
            self._editor_carrinho.destroy()
        idx = int(iid)
        x, y, w, h = bbox
        ent = tk.Entry(self.tree_carrinho, justify="center", font=("Arial", 11, "bold"), bg="#1f1f1f", fg="white",
                       insertbackground="white", relief="flat", highlightthickness=2, highlightcolor="#1f6aa5")
        ent.insert(0, self._formatar_qtd(self.carrinho[idx]['q']))
        ent.select_range(0, "end")
        ent.place(x=x, y=y, width=max(w, 60), height=h)
        ent.focus_set()
        self._editor_carrinho = ent
        estado = {"feito": False}

        def fechar(salvar):
            if estado["feito"]:
                return
            estado["feito"] = True
            texto = ent.get().strip()
            ent.destroy()
            self._editor_carrinho = None
            if not salvar or texto == "" or idx >= len(self.carrinho):
                return
            q = self.safe_float(texto)
            if q <= 0:
                removido = self.carrinho.pop(idx)
                self.notificar(f"Removido: {removido['n']}", "info", 4)
            else:
                self.carrinho[idx]['q'] = q
                self.carrinho[idx]['t'] = q * self.carrinho[idx]['vu']
            self.renderizar_carrinho()
            self.tree_carrinho.focus_set()

        ent.bind("<Return>", lambda e: fechar(True))
        ent.bind("<KP_Enter>", lambda e: fechar(True))
        ent.bind("<Escape>", lambda e: fechar(False))
        ent.bind("<FocusOut>", lambda e: fechar(True))
        return "break"

    def excluir_item_carrinho(self, event=None):
        sel = self.tree_carrinho.selection()
        if not sel:
            return
        removido = self.carrinho.pop(int(sel[0]))
        self.notificar(f"Removido: {removido['n']}", "info", 4)
        self.renderizar_carrinho()

    def atualizar_totais(self):
        tx = self.safe_float(self.ent_taxa_entrega.get())
        pg = self.safe_float(self.ent_valor_pago.get())
        tot = self.total_itens + tx
        if self.forma_pagamento.get() == "Dinheiro" and pg > 0:
            troco = pg - tot
            if troco >= -0.005:
                self.lbl_troco.configure(text=f"TROCO: R$ {troco:.2f}", text_color="green")
            else:
                self.lbl_troco.configure(text=f"FALTA: R$ {-troco:.2f}", text_color="#ff5555")
        else:
            self.lbl_troco.configure(text="TROCO: --", text_color="green")
        self.lbl_total.configure(text=f"TOTAL: R$ {tot:.2f}")

    # --- MODO EDIÇÃO DE VENDA ---
    def _mostrar_modo_edicao(self, venda_id):
        self.lbl_modo_edicao.configure(text=f"EDITANDO VENDA #{int(venda_id):04d}")
        self.f_modo_edicao.pack(fill="x", padx=10, pady=5, after=self.f_drive)
        self.btn_salvar_apenas.configure(text="SALVAR ALTERAÇÃO (F4)")
        self.btn_finalizar.configure(text="SALVAR E IMPRIMIR (F5)", fg_color="red")

    def _limpar_venda(self):
        self.carrinho = []
        self.renderizar_carrinho()
        self.ent_cli.delete(0, 'end'); self.ent_end.delete(0, 'end')
        self.ent_taxa_entrega.delete(0, 'end'); self.ent_valor_pago.delete(0, 'end')
        self.ent_qtd.delete(0, 'end'); self.ent_qtd.insert(0, "1")
        self.id_venda_em_edicao = None
        self.f_modo_edicao.pack_forget()
        self.btn_salvar_apenas.configure(text="SALVAR (F4)")
        self.btn_finalizar.configure(text="IMPRIMIR (F5)", fg_color="#2196F3")
        self.atualizar_totais()

    def cancelar_edicao_venda(self):
        if not self.id_venda_em_edicao:
            return
        if messagebox.askyesno("Cancelar edição", "Descartar as alterações desta venda?\n(A venda original continua salva como estava.)"):
            self._limpar_venda()
            self.notificar("Edição cancelada.", "info")

    def finalizar_venda(self, imprimir=True):
        if self._salvando_venda:
            return self.notificar("Aguarde: a venda anterior ainda está sendo salva.", "aviso")
        if not self.carrinho:
            return self.notificar("Carrinho vazio!", "aviso")

        cli = normalizar_texto(self.ent_cli.get()) or "CONSUMIDOR"
        end = " ".join((self.ent_end.get() or "").upper().split())
        pgto = self.forma_pagamento.get()
        tx = self.safe_float(self.ent_taxa_entrega.get())
        if tx < 0:
            return messagebox.showwarning("Taxa", "A taxa de entrega não pode ser negativa.")
        itens = [{k: v for k, v in item.items() if not k.startswith("_")} for item in self.carrinho]
        subtotal = sum(self.safe_float(i.get('t')) for i in itens)
        total = subtotal + tx
        val_pago = self.safe_float(self.ent_valor_pago.get())
        id_edicao = self.id_venda_em_edicao

        if pgto == "Dinheiro" and 0 < val_pago < total - 0.005:
            if not messagebox.askyesno("Valor recebido",
                                       f"O valor recebido (R$ {val_pago:.2f}) é MENOR que o total (R$ {total:.2f}).\nSalvar mesmo assim?"):
                return

        venda = {"cliente": cli, "endereco": end, "forma": pgto, "taxa": tx, "total": total,
                 "itens": itens, "id_edicao": id_edicao}

        def concluido(res):
            self._salvando_venda = False
            venda_id, data_venda = res
            if imprimir:
                self.imprimir(self.gerar_layout_cupom(venda_id, cli, end, itens, subtotal, tx, total, pgto,
                                                      val_pago, data_hora=data_venda))
            self._limpar_venda()
            self._agendar_refresh_geral()
            acao = "alterada" if id_edicao else "salva"
            self.notificar(f"✅ Venda #{venda_id:04d} {acao} — R$ {total:.2f}" + (" (imprimindo...)" if imprimir else ""), "ok", 10)
            self.ent_prod_venda.focus_set()

        def falhou(exc):
            self._salvando_venda = False
            messagebox.showerror("Erro ao salvar a venda", f"A venda NÃO foi salva.\n\n{exc}")

        self._salvando_venda = True
        self.notificar("Salvando venda...", "info")
        self.banco.enviar(bd.registrar_venda, venda, ok=concluido, erro=falhou)

    # =========================================================================
    # PRODUTOS E ESTOQUE
    # =========================================================================
    def salvar_produto(self):
        nome = normalizar_texto(self.ent_p_nome.get())
        preco = self.safe_float(self.ent_p_preco.get())
        if not nome:
            return self.notificar("Digite o nome do produto.", "aviso")
        if preco < 0:
            return messagebox.showwarning("Produto", "O preço não pode ser negativo.")
        if preco == 0 and not messagebox.askyesno("Preço zerado", "O preço está R$ 0,00. Salvar assim mesmo?"):
            return
        id_edicao = self.safe_int(self.id_prod_em_edicao) or None

        def concluido(nome_duplicado):
            if nome_duplicado:
                return messagebox.showwarning("Produto já existe",
                    f"Já existe um produto chamado '{nome_duplicado}'.\nUse EDITAR na lista para alterá-lo, em vez de cadastrar de novo.")
            self.notificar(f"Produto {'alterado' if id_edicao else 'cadastrado'}: {nome}", "ok")
            self.cancelar_edicao_prod()
            self._agendar_refresh_geral()

        self.banco.enviar(bd.salvar_produto, id_edicao, nome, preco, ok=concluido)

    def dar_entrada_estoque(self):
        nome = self.ent_entrada_busca.get()
        qtd = self.safe_int(self.ent_entrada_qtd.get())
        if not normalizar_texto(nome) or qtd <= 0:
            return self.notificar("Escolha o produto e digite uma quantidade maior que zero.", "aviso")

        def concluido(res):
            if res is None:
                return messagebox.showerror("Erro", "Produto não cadastrado.")
            nome_real, novo_estoque = res
            self.notificar(f"🚚 +{qtd} un de {nome_real}. Estoque agora: {self._formatar_qtd(novo_estoque)}", "ok", 10)
            self.lbl_estoque_entrada.configure(text=f"✅ Registrado: +{qtd} un de {nome_real} → estoque {self._formatar_qtd(novo_estoque)} un",
                                               text_color="#4CAF50")
            self.ent_entrada_busca.delete(0, 'end'); self.ent_entrada_qtd.delete(0, 'end')
            self.ent_entrada_busca.focus_set()
            self._agendar_refresh_geral()

        self.banco.enviar(bd.entrada_estoque, nome, qtd, ok=concluido)

    def excluir_produtos_selecionados(self):
        ids = self._ids_selecionados(self.tree_prod)
        if not ids:
            return
        if messagebox.askyesno("Confirmar", f"Excluir {len(ids)} produto(s)?\n(As vendas antigas continuam no histórico.)"):
            if self.safe_int(self.id_prod_em_edicao) in ids:
                self.cancelar_edicao_prod()
            self.banco.enviar(bd.excluir_produtos, ids, ok=lambda _: (self.notificar(f"{len(ids)} produto(s) excluído(s).", "ok"),
                                                                       self.atualizar_lista_produtos()))

    def carregar_produto_para_edicao(self):
        sel = self.tree_prod.selection()
        if len(sel) != 1:
            return messagebox.showwarning("Erro", "Selecione apenas 1 produto")
        pid, nome, preco = self.tree_prod.item(sel[0], "values")[:3]
        self.ent_p_nome.delete(0, 'end'); self.ent_p_nome.insert(0, nome)
        self.ent_p_preco.delete(0, 'end'); self.ent_p_preco.insert(0, f"{self.safe_float(preco):.2f}")
        self.id_prod_em_edicao = self.safe_int(pid)
        self.btn_salvar_prod.configure(text="SALVAR ALTERAÇÕES", fg_color="blue")
        self.btn_cancelar_prod.pack(side="left", padx=2)

    def cancelar_edicao_prod(self):
        self.id_prod_em_edicao = None
        self.ent_p_nome.delete(0, 'end'); self.ent_p_preco.delete(0, 'end')
        self.btn_salvar_prod.configure(text="SALVAR / CRIAR", fg_color="green")
        self.btn_cancelar_prod.pack_forget()

    # =========================================================================
    # LISTAS (sempre carregadas pela thread do banco; respostas velhas são
    # descartadas pelo número de sequência)
    # =========================================================================
    def atualizar_lista_produtos(self):
        self._agendar_refresh_lista("produtos", self._buscar_lista_produtos)

    def _buscar_lista_produtos(self):
        seq = self._novo_seq("produtos")
        self.banco.enviar(bd.listar_produtos, self.ent_busca_prod.get(), ok=lambda rows: self._desenhar_produtos(rows, seq))

    def _desenhar_produtos(self, rows, seq):
        if seq != self._seq_listas.get("produtos"):
            return
        self.tree_prod.delete(*self.tree_prod.get_children())
        so_baixo = self.var_so_baixo.get()
        n_baixo = 0
        for pid, nome, preco, estoque, saidas in rows:
            baixo = estoque < ESTOQUE_MINIMO
            n_baixo += baixo
            if so_baixo and not baixo:
                continue
            self.tree_prod.insert("", "end", tags=("baixo" if baixo else "normal",),
                                  values=(pid, nome, f"R$ {preco:.2f}", self._formatar_qtd(estoque), self._formatar_qtd(saidas)))
        self.lbl_resumo_estoque.configure(text=f"{len(rows)} produto(s)  •  {n_baixo} com estoque baixo (em vermelho)")

    # --- ENTRADA DE MERCADORIA ---
    def atualizar_lista_entradas(self):
        self._agendar_refresh_lista("entradas", self._buscar_lista_entradas)

    def _buscar_lista_entradas(self):
        seq = self._novo_seq("entradas")
        self.banco.enviar(bd.listar_entradas, self.ent_busca_entrada.get(), self.ent_data_entrada.get().strip(),
                          ok=lambda rows: self._desenhar_entradas(rows, seq))

    def _desenhar_entradas(self, rows, seq):
        if seq != self._seq_listas.get("entradas"):
            return
        self.tree_ent.delete(*self.tree_ent.get_children())
        for eid, data, nome, qtd in rows:
            self.tree_ent.insert("", "end", values=(eid, data, nome, self._formatar_qtd(qtd)))

    def _mostrar_estoque_entrada(self):
        nome = self.ent_entrada_busca.get()

        def mostrar(res):
            if self.ent_entrada_busca.get() != nome:
                return
            if res:
                self.lbl_estoque_entrada.configure(text=f"Estoque atual de {res[1]}: {self._formatar_qtd(res[3])} un",
                                                   text_color="#ff5555" if res[3] < ESTOQUE_MINIMO else "white")
            else:
                self.lbl_estoque_entrada.configure(text="Produto não cadastrado (cadastre na aba Estoque).", text_color="#ff5555")

        self.banco.enviar(bd.buscar_produto, nome, ok=mostrar)

    def _ir_para_entrada_do_selecionado(self):
        sel = self.tree_prod.selection()
        if len(sel) != 1:
            return messagebox.showwarning("Entrada", "Selecione 1 produto na lista.")
        nome = self.tree_prod.item(sel[0], "values")[1]
        self.abas.set("Entrada de Mercadoria")
        self.ent_entrada_busca.delete(0, "end"); self.ent_entrada_busca.insert(0, nome)
        self._mostrar_estoque_entrada()
        self.ent_entrada_qtd.focus_set()

    def excluir_entradas_selecionadas(self):
        ids = self._ids_selecionados(self.tree_ent)
        if not ids:
            return
        if messagebox.askyesno("Confirmar", f"Excluir {len(ids)} entrada(s) de mercadoria?\n\n"
                                            "A quantidade dessas entradas será RETIRADA do estoque."):
            self.banco.enviar(bd.excluir_entradas, ids, ok=lambda _: (self.notificar(f"{len(ids)} entrada(s) excluída(s); estoque corrigido.", "ok"),
                                                                       self._agendar_refresh_geral()))

    def atualizar_lista_clientes(self):
        self._agendar_refresh_lista("clientes", self._buscar_lista_clientes)

    def _buscar_lista_clientes(self):
        seq = self._novo_seq("clientes")
        self.banco.enviar(bd.listar_clientes, self.ent_busca_cli.get(), ok=lambda rows: self._desenhar_clientes(rows, seq))

    def _desenhar_clientes(self, rows, seq):
        if seq != self._seq_listas.get("clientes"):
            return
        self.tree_cli.delete(*self.tree_cli.get_children())
        for cid, nome, end in rows:
            self.tree_cli.insert("", "end", values=(cid, nome, end))

    def atualizar_lista_vendas(self):
        self._agendar_refresh_lista("vendas", self._buscar_lista_vendas)

    def _buscar_lista_vendas(self):
        seq = self._novo_seq("vendas")
        self.banco.enviar(bd.listar_vendas, self.ent_id_rel.get().strip(), self.ent_data_rel.get().strip(),
                          self.ent_cli_rel.get(), ok=lambda rows: self._desenhar_vendas(rows, seq))

    def _desenhar_vendas(self, rows, seq):
        if seq != self._seq_listas.get("vendas"):
            return
        self.tree_rel.delete(*self.tree_rel.get_children())
        for vid, d, c, t, f in rows:
            self.tree_rel.insert("", "end", values=(vid, d, c, f"R$ {t:.2f}", f))

    # =========================================================================
    # CLIENTES
    # =========================================================================
    def salvar_cliente(self):
        nome = normalizar_texto(self.ent_c_nome.get())
        end = " ".join(self.ent_c_end.get().upper().split())
        if not nome:
            return self.notificar("Digite o nome do cliente.", "aviso")
        id_edicao = self.safe_int(self.id_cliente_em_edicao) or None

        def concluido(nome_duplicado):
            if nome_duplicado:
                return messagebox.showwarning("Cliente já existe",
                    f"Já existe um cliente chamado '{nome_duplicado}'.\nUse EDITAR na lista para alterá-lo, em vez de cadastrar de novo.")
            self.notificar(f"Cliente {'alterado' if id_edicao else 'cadastrado'}: {nome}", "ok")
            self.cancelar_edicao_cli()
            self._agendar_refresh_geral()

        self.banco.enviar(bd.salvar_cliente, id_edicao, nome, end, ok=concluido)

    def buscar_endereco_cliente(self):
        nome = self.ent_cli.get()
        if not normalizar_texto(nome):
            return

        def preencher(res):
            # Só preenche se o nome no campo ainda é o mesmo e o endereço está vazio
            if res and self.ent_cli.get() == nome and not self.ent_end.get().strip():
                self.ent_end.insert(0, res[2])

        self.banco.enviar(bd.buscar_cliente, nome, ok=preencher)

    def excluir_clientes_selecionados(self):
        ids = self._ids_selecionados(self.tree_cli)
        if not ids:
            return
        if messagebox.askyesno("Confirmar", f"Excluir {len(ids)} cliente(s)?"):
            if self.safe_int(self.id_cliente_em_edicao) in ids:
                self.cancelar_edicao_cli()
            self.banco.enviar(bd.excluir_clientes, ids, ok=lambda _: (self.notificar(f"{len(ids)} cliente(s) excluído(s).", "ok"),
                                                                       self.atualizar_lista_clientes()))

    def carregar_cliente_para_edicao(self):
        sel = self.tree_cli.selection()
        if len(sel) != 1:
            return messagebox.showwarning("Erro", "Selecione apenas 1 cliente")
        val = self.tree_cli.item(sel[0], "values")
        self.ent_c_nome.delete(0, 'end'); self.ent_c_nome.insert(0, val[1])
        self.ent_c_end.delete(0, 'end'); self.ent_c_end.insert(0, val[2] if len(val) > 2 else "")
        self.id_cliente_em_edicao = self.safe_int(val[0])
        self.btn_salvar_cli.configure(text="SALVAR ALTERAÇÕES", fg_color="blue")
        self.btn_cancelar_cli.pack(side="left", padx=2)

    def cancelar_edicao_cli(self):
        self.id_cliente_em_edicao = None; self.ent_c_nome.delete(0, 'end'); self.ent_c_end.delete(0, 'end')
        self.btn_salvar_cli.configure(text="SALVAR", fg_color="green"); self.btn_cancelar_cli.pack_forget()

    # =========================================================================
    # VENDAS ANTIGAS
    # =========================================================================
    def excluir_vendas_selecionadas(self):
        ids = self._ids_selecionados(self.tree_rel)
        if not ids:
            return
        if self.id_venda_em_edicao and self.safe_int(self.id_venda_em_edicao) in ids:
            return messagebox.showwarning("Venda em edição",
                                          "Uma das vendas selecionadas está aberta para edição.\nCancele a edição antes de excluir.")
        if not messagebox.askyesno("CUIDADO", f"Apagar {len(ids)} venda(s)?\n\nOs produtos dessas vendas VOLTARÃO para o estoque."):
            return
        self.banco.enviar(bd.excluir_vendas, ids, ok=lambda _: (self.notificar(f"{len(ids)} venda(s) excluída(s); estoque devolvido.", "ok"),
                                                                 self._agendar_refresh_geral()))

    def reimprimir_cupom(self):
        ids = self._ids_selecionados(self.tree_rel)
        if len(ids) != 1:
            return messagebox.showwarning("Reimprimir", "Selecione 1 venda para reimprimir.")

        def com_venda(v):
            if not v:
                return messagebox.showwarning("Venda", "Registro da venda não foi encontrado.")
            cupom = self.gerar_layout_cupom(v["id"], v["cliente"], v["endereco"], v["itens"], v["total"] - v["taxa"],
                                            v["taxa"], v["total"], v["forma"], data_hora=v["data"], reimpressao=True)
            self.imprimir(cupom)

        self.banco.enviar(bd.ler_venda, ids[0], ok=com_venda)

    def editar_venda_antiga(self):
        ids = self._ids_selecionados(self.tree_rel)
        if len(ids) != 1:
            return messagebox.showwarning("Editar", "Selecione 1 venda para editar.")
        aviso = "\n\nATENÇÃO: os itens que estão agora no carrinho serão descartados." if self.carrinho else ""
        if not messagebox.askyesno("Confirmar", f"Editar a venda #{ids[0]:04d}?{aviso}"):
            return
        self.banco.enviar(bd.ler_venda, ids[0], ok=self._carregar_venda_para_edicao)

    def _carregar_venda_para_edicao(self, v):
        if not v:
            return messagebox.showwarning("Venda", "Registro da venda não foi encontrado.")
        self._limpar_venda()
        self.id_venda_em_edicao = v["id"]
        self.ent_cli.insert(0, v["cliente"])
        self.ent_end.insert(0, v["endereco"])
        self.forma_pagamento.set(v["forma"] or "Dinheiro")
        if v["taxa"]:
            self.ent_taxa_entrega.insert(0, f"{float(v['taxa']):.2f}")
        self.carrinho = v["itens"]
        self.renderizar_carrinho()
        self._mostrar_modo_edicao(v["id"])
        self.abas.set("Venda")
        self.notificar(f"Editando a venda #{v['id']:04d}. Salve com F4/F5 ou cancele.", "aviso", 10)


if __name__ == "__main__":
    app = SistemaComanda()
    app.mainloop()
