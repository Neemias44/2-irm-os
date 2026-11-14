"""
Painel (dashboard) da aba Relatórios.

Gráficos desenhados direto num tk.Canvas: não precisa instalar nada
(nem matplotlib) e redesenha em milissegundos quando o período muda.

Cores: paleta de referência para tema escuro (superfície #1a1a19).
  * dinheiro (R$)  -> azul   #3987e5
  * quantidades    -> verde-água #199e70
  * comparação ▲/▼ -> verde/vermelho de "status", sempre com seta + texto
    (a cor nunca é a única pista).
"""
import math
import tkinter as tk
import tkinter.font as tkfont

import customtkinter as ctk

# --- Cores (tema escuro) ---
SUPERFICIE = "#1a1a19"
TINTA = "#f0efec"          # texto principal
TINTA_2 = "#c3c2b7"        # texto secundário
APAGADO = "#898781"        # eixos e rótulos
GRADE = "#2c2c2a"          # linhas de grade
BASE = "#383835"           # linha de base
COR_DINHEIRO = "#3987e5"
COR_DINHEIRO_HOVER = "#6da7ec"
COR_QTD = "#199e70"
COR_QTD_HOVER = "#3fc795"
BOM = "#0ca30c"
RUIM = "#d03b3b"
FONTE = "Segoe UI"


# =============================================================================
# FORMATAÇÃO
# =============================================================================
def moeda(valor, simbolo=True):
    """1234.5 -> 'R$ 1.234,50'"""
    texto = f"{valor:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"R$ {texto}" if simbolo else texto


def moeda_curta(valor):
    """Para eixos: 950 -> 'R$ 950', 1250 -> 'R$ 1,3 mil', 2500000 -> 'R$ 2,5 mi'."""
    if abs(valor) >= 1_000_000:
        return f"R$ {valor / 1_000_000:.1f} mi".replace(".", ",").replace(",0 ", " ")
    if abs(valor) >= 1_000:
        return f"R$ {valor / 1_000:.1f} mil".replace(".", ",").replace(",0 ", " ")
    return f"R$ {valor:.0f}"


def numero(valor):
    v = float(valor)
    if v.is_integer():
        return f"{int(v):,}".replace(",", ".")
    return f"{v:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")


def _passo_bonito(bruto):
    """Arredonda o intervalo da grade para 1, 2, 2.5 ou 5 x 10^n."""
    if bruto <= 0:
        return 1
    exp = 10 ** math.floor(math.log10(bruto))
    for m in (1, 2, 2.5, 5, 10):
        if bruto <= m * exp:
            return m * exp
    return 10 * exp


def _caber(fonte, texto, largura):
    """Corta o texto com '…' até caber na largura (em pixels)."""
    if fonte.measure(texto) <= largura:
        return texto
    while texto and fonte.measure(texto + "…") > largura:
        texto = texto[:-1]
    return texto + "…"


# =============================================================================
# TOOLTIP DENTRO DO CANVAS
# =============================================================================
class _Dica:
    def __init__(self, canvas):
        self.c = canvas

    def mostrar(self, x, y, linhas):
        self.esconder()
        texto = "\n".join(linhas)
        # state="disabled": o balão é "transparente" para o mouse. Sem isso, o balão
        # aparecia sob o ponteiro, o Tk entendia que o mouse SAIU da coluna, o balão
        # era apagado, o mouse ENTRAVA de novo... ciclo infinito que travava a tela.
        t = self.c.create_text(0, 0, text=texto, anchor="nw", fill=TINTA, font=(FONTE, 10), tags="dica", state="disabled")
        x0, y0, x1, y1 = self.c.bbox(t)
        lw, lh = x1 - x0 + 16, y1 - y0 + 12
        w, h = self.c.winfo_width(), self.c.winfo_height()
        px = min(max(4, x - lw / 2), w - lw - 4)
        py = y - lh - 10
        if py < 4:
            py = min(y + 14, h - lh - 4)
        self.c.coords(t, px + 8, py + 6)
        fundo = self.c.create_rectangle(px, py, px + lw, py + lh, fill="#2c2c2a", outline="#4a4a47", tags="dica",
                                        state="disabled")
        self.c.tag_lower(fundo, t)

    def esconder(self):
        self.c.delete("dica")


# =============================================================================
# GRÁFICO DE COLUNAS (série no tempo)
# =============================================================================
class GraficoColunas(tk.Canvas):
    """dados: [(rótulo curto, valor, [linhas da dica])]"""

    def __init__(self, master, cor=COR_DINHEIRO, cor_hover=COR_DINHEIRO_HOVER, formatar_eixo=moeda_curta, **kw):
        super().__init__(master, bg=SUPERFICIE, highlightthickness=0, **kw)
        self.cor, self.cor_hover, self.formatar_eixo = cor, cor_hover, formatar_eixo
        self.dados = []
        self.msg_vazio = "Sem vendas no período"
        self.dica = _Dica(self)
        self._ativo = None
        self.bind("<Configure>", lambda e: self.desenhar())

    def atualizar(self, dados):
        self.dados = dados
        self.desenhar()

    def desenhar(self):
        self.delete("all")
        self._ativo = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60 or h < 60:
            return
        valores = [d[1] for d in self.dados]
        vmax = max(valores, default=0)
        # Grade e eixo Y (4 divisões "bonitas")
        passo = _passo_bonito(vmax / 4) if vmax > 0 else 1
        teto = passo * max(1, math.ceil(vmax / passo)) if vmax > 0 else 4
        n_linhas = int(round(teto / passo)) if vmax > 0 else 4

        # Margens medidas pelo tamanho REAL do texto (funciona com zoom do Windows em 125%/150%)
        f_eixo = tkfont.Font(family=FONTE, size=9)
        rotulos_y = [self.formatar_eixo(passo * i) for i in range(n_linhas + 1)] if vmax > 0 else ["R$ 0"]
        esq = max(f_eixo.measure(r) for r in rotulos_y) + 16
        alt_txt = f_eixo.metrics("linespace")
        dir_, topo, baixo = 14, alt_txt + 10, alt_txt + 10
        pw, ph = w - esq - dir_, h - topo - baixo
        base_y = topo + ph
        if pw < 40 or ph < 30:
            return
        for i in range(n_linhas + 1):
            y = base_y - ph * i / n_linhas
            if i > 0:
                self.create_line(esq, y, w - dir_, y, fill=GRADE, state="disabled")
            rotulo = self.formatar_eixo(passo * i) if vmax > 0 else ""
            self.create_text(esq - 8, y, text=rotulo, anchor="e", fill=APAGADO, font=(FONTE, 9), state="disabled")

        if not self.dados or vmax <= 0:
            self.create_line(esq, base_y, w - dir_, base_y, fill=BASE, state="disabled")
            self.create_text(esq + pw / 2, topo + ph / 2, text=self.msg_vazio, fill=APAGADO, font=(FONTE, 12))
            return

        n = len(self.dados)
        slot = pw / n
        larg = max(2, min(56, slot - max(2, slot * 0.28)))   # sempre >= 2px de espaço entre colunas
        # Rótulos do eixo X sem encavalar
        larg_rotulo_x = max(f_eixo.measure(d[0]) for d in self.dados) + 10
        pular = max(1, math.ceil(n * larg_rotulo_x / pw))
        i_max = valores.index(vmax)

        for i, (rotulo, valor, dica) in enumerate(self.dados):
            cx = esq + slot * i + slot / 2
            tag = f"c{i}"
            # Área de toque: a coluna inteira (mais fácil de acertar que a barra)
            self.create_rectangle(esq + slot * i, topo, esq + slot * (i + 1), base_y, fill=SUPERFICIE, outline="", tags=(tag,))
            if valor > 0:
                y = base_y - ph * valor / teto
                self.create_rectangle(cx - larg / 2, y, cx + larg / 2, base_y, fill=self.cor, outline="", tags=(tag, "barra"))
            if i % pular == 0:
                self.create_text(cx, base_y + alt_txt / 2 + 4, text=rotulo, fill=APAGADO, font=(FONTE, 9), state="disabled")
            self.tag_bind(tag, "<Enter>", lambda e, i=i: self._entrar(i))
            self.tag_bind(tag, "<Leave>", lambda e, i=i: self._sair(i))

        # Rótulo direto só no maior valor (não em todas as colunas)
        y_max = base_y - ph * vmax / teto
        self.create_text(esq + slot * i_max + slot / 2, y_max - alt_txt / 2 - 2, text=self.formatar_eixo(vmax),
                         fill=TINTA_2, font=(FONTE, 9, "bold"), state="disabled")
        # Linhas de grade ficam por cima das áreas de toque; a base por cima de tudo
        self.tag_raise("barra")
        self.create_line(esq, base_y, w - dir_, base_y, fill=BASE, state="disabled")

    def _entrar(self, i):
        if self._ativo == i:      # já está mostrando esta coluna: não redesenha
            return
        self._ativo = i
        self.itemconfigure(f"c{i}&&barra", fill=self.cor_hover)
        bbox = self.bbox(f"c{i}")
        x = (bbox[0] + bbox[2]) / 2
        barras = self.find_withtag(f"c{i}&&barra")
        y = self.coords(barras[0])[1] if barras else self.winfo_height() - 30
        self.dica.mostrar(x, y, self.dados[i][2])

    def _sair(self, i):
        if self._ativo == i:
            self._ativo = None
        self.itemconfigure(f"c{i}&&barra", fill=self.cor)
        self.dica.esconder()


# =============================================================================
# GRÁFICO DE BARRAS HORIZONTAIS (categorias)
# =============================================================================
class GraficoBarrasH(tk.Canvas):
    """linhas: [(rótulo, valor, texto à direita da barra, [linhas da dica])]"""

    def __init__(self, master, cor=COR_DINHEIRO, cor_hover=COR_DINHEIRO_HOVER, **kw):
        super().__init__(master, bg=SUPERFICIE, highlightthickness=0, **kw)
        self.cor, self.cor_hover = cor, cor_hover
        self.todas = []
        self.linhas = []
        self.msg_vazio = "Sem dados no período"
        # agrupar_resto(lista de linhas que não couberam) -> uma linha "Outros"
        self.agrupar_resto = None
        self.dica = _Dica(self)
        self._ativo = None
        self.bind("<Configure>", lambda e: self.desenhar())

    def atualizar(self, linhas):
        self.todas = linhas
        self.desenhar()

    def desenhar(self):
        self.delete("all")
        self._ativo = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 60 or h < 30:
            return
        if not self.todas:
            self.linhas = []
            self.create_text(w / 2, h / 2, text=self.msg_vazio, fill=APAGADO, font=(FONTE, 12))
            return
        fonte = tkfont.Font(family=FONTE, size=10)
        topo = 4
        # Só mostra as linhas que cabem com folga; o resto vira uma linha "Outros"
        alt_min = fonte.metrics("linespace") + 6
        cabem = max(1, int((h - topo * 2) // alt_min))
        if len(self.todas) > cabem and self.agrupar_resto and cabem >= 2:
            self.linhas = self.todas[:cabem - 1] + [self.agrupar_resto(self.todas[cabem - 1:])]
        else:
            self.linhas = self.todas[:cabem]
        n = len(self.linhas)
        alt_linha = min(fonte.metrics("linespace") + 12, (h - topo * 2) / n)
        alt_barra = max(4, alt_linha - 10)
        # Coluna dos nomes: do tamanho do maior nome, mas no máximo 35% da largura
        x0 = min(max(fonte.measure(l[0]) for l in self.linhas) + 20, w * 0.35)
        area_valor = max(fonte.measure(l[2]) for l in self.linhas) + 16
        pw = max(20, w - x0 - area_valor)
        vmax = max(l[1] for l in self.linhas) or 1
        self._x0 = x0

        for i, (rotulo, valor, texto_valor, dica, *_) in enumerate(self.linhas):
            tag = f"l{i}"
            yc = topo + alt_linha * i + alt_linha / 2
            self.create_rectangle(0, yc - alt_linha / 2, w, yc + alt_linha / 2, fill=SUPERFICIE, outline="", tags=(tag,))
            self.create_text(x0 - 10, yc, text=_caber(fonte, rotulo, x0 - 16), anchor="e", fill=TINTA_2,
                             font=(FONTE, 10), state="disabled")
            larg = max(2, pw * valor / vmax) if valor > 0 else 0
            if larg:
                self.create_rectangle(x0, yc - alt_barra / 2, x0 + larg, yc + alt_barra / 2, fill=self.cor,
                                      outline="", tags=(tag, "barra"))
            self.create_text(x0 + larg + 8, yc, text=texto_valor, anchor="w", fill=TINTA, font=(FONTE, 10), state="disabled")
            self.tag_bind(tag, "<Enter>", lambda e, i=i: self._entrar(i))
            self.tag_bind(tag, "<Leave>", lambda e, i=i: self._sair(i))
        self.create_line(x0, topo, x0, topo + alt_linha * n, fill=BASE, state="disabled")

    def _entrar(self, i):
        if self._ativo == i:
            return
        self._ativo = i
        self.itemconfigure(f"l{i}&&barra", fill=self.cor_hover)
        bbox = self.bbox(f"l{i}")
        self.dica.mostrar(getattr(self, "_x0", 120) + 120, bbox[1] + 4, self.linhas[i][3])

    def _sair(self, i):
        if self._ativo == i:
            self._ativo = None
        self.itemconfigure(f"l{i}&&barra", fill=self.cor)
        self.dica.esconder()


# =============================================================================
# CARTÃO DE INDICADOR (KPI)
# =============================================================================
class CartaoKPI(ctk.CTkFrame):
    def __init__(self, master, titulo, formatar, subir_e_bom=True):
        super().__init__(master, fg_color=SUPERFICIE, corner_radius=8)
        self.formatar, self.subir_e_bom = formatar, subir_e_bom
        ctk.CTkLabel(self, text=titulo, text_color=APAGADO, font=(FONTE, 12), anchor="w").pack(fill="x", padx=12, pady=(8, 0))
        self.lbl_valor = ctk.CTkLabel(self, text="—", text_color=TINTA, font=(FONTE, 22, "bold"), anchor="w")
        self.lbl_valor.pack(fill="x", padx=12)
        self.lbl_delta = ctk.CTkLabel(self, text="", text_color=APAGADO, font=(FONTE, 11), anchor="w")
        self.lbl_delta.pack(fill="x", padx=12, pady=(0, 8))

    def mostrar(self, valor, anterior, rotulo_anterior):
        self.lbl_valor.configure(text=self.formatar(valor))
        feminino = rotulo_anterior.startswith("semana")
        if not anterior:
            texto, cor = (f"sem vendas {'na' if feminino else 'no'} {rotulo_anterior}" if valor else "—"), APAGADO
        else:
            var = (valor - anterior) / anterior * 100
            if abs(var) < 0.05:
                texto, cor = f"= igual {'à' if feminino else 'ao'} {rotulo_anterior}", APAGADO
            else:
                subiu = var > 0
                seta = "▲" if subiu else "▼"
                bom = subiu == self.subir_e_bom
                cor = BOM if bom else RUIM
                if self.subir_e_bom is None:
                    cor = APAGADO
                texto = f"{seta} {abs(var):.1f}% vs {rotulo_anterior}".replace(".", ",")
        self.lbl_delta.configure(text=texto, text_color=cor)

    @property
    def texto_valor(self):
        return self.lbl_valor.cget("text")


# =============================================================================
# PAINEL COMPLETO
# =============================================================================
class Painel(ctk.CTkFrame):
    TITULOS_SERIE = {"hora": "Faturamento por hora", "dia": "Faturamento por dia", "mes": "Faturamento por mês"}

    def __init__(self, master):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure((0, 1), weight=1, uniform="col")
        # Altura base pequena nos gráficos + pesos: o espaço livre é repartido 55% / 45%
        self.grid_rowconfigure(1, weight=11, minsize=180)
        self.grid_rowconfigure(2, weight=9, minsize=170)

        # Linha 1: indicadores
        f_kpi = ctk.CTkFrame(self, fg_color="transparent")
        f_kpi.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.kpis = {
            "faturamento": CartaoKPI(f_kpi, "Faturamento", moeda),
            "vendas": CartaoKPI(f_kpi, "Vendas", numero),
            "ticket": CartaoKPI(f_kpi, "Ticket médio", moeda),
            "itens": CartaoKPI(f_kpi, "Itens vendidos", numero),
            "entregas": CartaoKPI(f_kpi, "Entregas (com taxa)", numero),
            "taxas": CartaoKPI(f_kpi, "Taxas de entrega", moeda),
        }
        for i, k in enumerate(self.kpis.values()):
            f_kpi.grid_columnconfigure(i, weight=1, uniform="kpi")
            k.grid(row=0, column=i, sticky="nsew", padx=4)

        # Linha 2: faturamento no tempo
        self.f_serie, self.lbl_serie, self.lbl_serie_sub = self._cartao_grafico(1, 0, colspan=2)
        self.g_serie = GraficoColunas(self.f_serie, height=120)
        self.g_serie.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Linha 3: formas de pagamento | produtos
        self.f_formas, self.lbl_formas, self.lbl_formas_sub = self._cartao_grafico(2, 0)
        self.lbl_formas.configure(text="Formas de pagamento")
        self.g_formas = GraficoBarrasH(self.f_formas, height=100)
        self.g_formas.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self.f_prod, self.lbl_prod, self.lbl_prod_sub = self._cartao_grafico(2, 1)
        self.lbl_prod.configure(text="Produtos mais vendidos (quantidade)")
        self.g_prod = GraficoBarrasH(self.f_prod, cor=COR_QTD, cor_hover=COR_QTD_HOVER, height=100)
        self.g_prod.msg_vazio = "Nenhum produto vendido no período"
        self.g_prod.agrupar_resto = self._agrupar_produtos
        self.g_prod.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    @staticmethod
    def _agrupar_produtos(resto):
        q, v = sum(r[1] for r in resto), sum(r[4] for r in resto)
        return (f"Outros ({len(resto)})", q, f"{numero(q)} un  •  {moeda(v)}",
                [f"Outros {len(resto)} produtos:"] + [f"  {r[0]}: {numero(r[1])} un" for r in resto[:10]]
                + ([f"  ... e mais {len(resto) - 10}"] if len(resto) > 10 else []), v)

    def _cartao_grafico(self, linha, coluna, colspan=1):
        f = ctk.CTkFrame(self, fg_color=SUPERFICIE, corner_radius=8)
        f.grid(row=linha, column=coluna, columnspan=colspan, sticky="nsew", padx=4, pady=4)
        topo = ctk.CTkFrame(f, fg_color="transparent")
        topo.pack(fill="x", padx=12, pady=(8, 2))
        titulo = ctk.CTkLabel(topo, text="", text_color=TINTA, font=(FONTE, 14, "bold"), anchor="w")
        titulo.pack(side="left")
        sub = ctk.CTkLabel(topo, text="", text_color=APAGADO, font=(FONTE, 11), anchor="e")
        sub.pack(side="right")
        return f, titulo, sub

    def mostrar(self, d, rotulo_anterior):
        at, ant = d["atual"], d["anterior"]
        for chave, cartao in self.kpis.items():
            cartao.mostrar(at[chave], ant[chave], rotulo_anterior)

        # Faturamento no tempo
        gran = d["granularidade"]
        self.lbl_serie.configure(text=self.TITULOS_SERIE[gran])
        dias_com_venda = sum(1 for s in d["serie"] if s[2])
        if gran == "hora":
            pico = max(d["serie"], key=lambda s: s[1], default=None)
            sub = f"Horário de pico: {pico[3]}" if pico and pico[1] > 0 else ""
        else:
            unidade = "dia" if gran == "dia" else "mês"
            media = at["faturamento"] / dias_com_venda if dias_com_venda else 0
            sub = f"Média por {unidade} com venda: {moeda(media)}" if dias_com_venda else ""
        self.lbl_serie_sub.configure(text=sub)
        self.g_serie.atualizar([(curto, valor, [longo, moeda(valor), f"{qtd} venda(s)"])
                                for curto, valor, qtd, longo in d["serie"]])

        # Formas de pagamento (as modalidades): valor e % do total
        total = at["faturamento"]
        self.lbl_formas_sub.configure(text=f"{len(d['formas'])} forma(s)" if d["formas"] else "")

        def agrupar_formas(resto):
            v = sum(r[1] for r in resto)
            pct = f"  ({v / total * 100:.0f}%)" if total else ""
            return (f"Demais ({len(resto)})", v, moeda(v) + pct,
                    [f"Demais formas:"] + [f"  {r[0]}: {moeda(r[1])}" for r in resto])

        self.g_formas.agrupar_resto = agrupar_formas
        self.g_formas.atualizar([
            (forma, valor, f"{moeda(valor)}  ({valor / total * 100:.0f}%)" if total else moeda(valor),
             [forma, moeda(valor), f"{qtd} venda(s)", f"{valor / total * 100:.1f}% do faturamento".replace(".", ",") if total else ""])
            for forma, qtd, valor in d["formas"]])

        # Produtos: os 8 mais vendidos; o resto vira "Outros"
        prods = d["produtos"]
        self.lbl_prod_sub.configure(text=f"{len(prods)} produto(s) diferentes" if prods else "")
        self.g_prod.atualizar([(n, q, f"{numero(q)} un  •  {moeda(v)}", [n, f"{numero(q)} unidade(s)", f"{moeda(v)} vendidos"], v)
                               for n, q, v in prods])
