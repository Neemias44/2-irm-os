import customtkinter as ctk
import sqlite3
import json
import os
import tempfile
import threading
import socket
import textwrap
from datetime import datetime
from tkinter import messagebox, filedialog, ttk

# --- TENTA IMPORTAR O REPORTLAB (PDF) ---
try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
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
    import win32api
    TEM_IMPRESSORA = True
except ImportError:
    TEM_IMPRESSORA = False

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
        self.lista_sugestoes = None 
        self.ip_local = self.pegar_ip()

        # Inicialização
        self.init_db()
        self.estilizar_treeview() 
        
        if TEM_FLASK:
            self.iniciar_servidor_celular()

        # Bind Global para fechar sugestões
        self.bind("<Button-1>", lambda e: self.verificar_foco_sugestao(e))

        # --- ABAS ---
        self.abas = ctk.CTkTabview(self)
        self.abas.pack(padx=10, pady=10, fill="both", expand=True)
        
        self.tab_venda = self.abas.add("Venda")
        self.tab_clientes = self.abas.add("Clientes")
        self.tab_produtos = self.abas.add("Estoque & Entrada")
        self.tab_relatorio = self.abas.add("Relatórios")

        self.montar_aba_venda()
        self.montar_aba_clientes()
        self.montar_aba_produtos()
        self.montar_aba_relatorio()

    # --- ESTILO DARK PARA TABELAS (Treeview) ---
    def estilizar_treeview(self):
        style = ttk.Style()
        style.theme_use("clam")
        
        style.configure("Treeview",
                        background="#2b2b2b",
                        foreground="white",
                        fieldbackground="#2b2b2b",
                        rowheight=30,
                        bordercolor="#2b2b2b",
                        borderwidth=0,
                        font=("Arial", 11))
        
        style.map("Treeview", background=[("selected", "#1f6aa5")])
        
        style.configure("Treeview.Heading",
                        background="#333333",
                        foreground="white",
                        relief="flat",
                        font=("Arial", 10, "bold"))
        
        style.map("Treeview.Heading", background=[("active", "#444444")])

    # --- UTILITÁRIOS ---
    def safe_float(self, valor):
        try:
            if not valor or str(valor).strip() == "": return 0.0
            return float(str(valor).replace(",", "."))
        except: return 0.0
            
    def safe_int(self, valor):
        try:
            if not valor or str(valor).strip() == "": return 0
            return int(float(str(valor)))
        except: return 0

    def pegar_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    # --- BANCO DE DADOS (THREAD SAFE) ---
    def get_db_connection(self):
        return sqlite3.connect("loja.db", timeout=30)

    def init_db(self):
        try:
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("CREATE TABLE IF NOT EXISTS prods (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, preco REAL)")
                cursor.execute("CREATE TABLE IF NOT EXISTS clientes (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, endereco TEXT)")
                cursor.execute("""CREATE TABLE IF NOT EXISTS historico 
                                (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, cliente TEXT, total REAL, forma TEXT, taxa REAL, itens_json TEXT)""")
                cursor.execute("""CREATE TABLE IF NOT EXISTS entradas_estoque 
                                (id INTEGER PRIMARY KEY AUTOINCREMENT, nome_prod TEXT, qtd INTEGER, data TEXT)""")

                cursor.execute("CREATE INDEX IF NOT EXISTS idx_prod_nome ON prods(nome)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_cli_nome ON clientes(nome)")

                try:
                    cursor.execute("SELECT estoque FROM prods LIMIT 1")
                except sqlite3.OperationalError:
                    cursor.execute("ALTER TABLE prods ADD COLUMN estoque INTEGER DEFAULT 0")
                
                conn.commit()
        except sqlite3.Error as e:
            messagebox.showerror("Erro de Banco", f"Erro ao inicializar: {e}")

    # --- SERVIDOR CELULAR (FLASK) ---
    def iniciar_servidor_celular(self):
        app = Flask(__name__)
        HTML_TEMPLATE = """
        <!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Estoque Dois Irmãos</title><meta http-equiv="refresh" content="30">
        <style>body{font-family:Arial,sans-serif;background:#222;color:#fff;padding:10px} .card{background:#333;padding:15px;margin-bottom:10px;border-left:5px solid #2196F3;display:flex;justify-content:space-between} .low{border-left-color:#f44336} .val{font-size:1.4em;font-weight:bold}</style>
        </head><body><h2 style="text-align:center;color:#4caf50">📦 Estoque</h2>
        {% for p in produtos %}
        <div class="card {% if p[2]<5 %}low{% endif %}"><div><b>{{p[0]}}</b><br><small>R$ {{ "%.2f"|format(p[1]) }}</small></div><div style="text-align:right"><span class="val">{{p[2]}}</span><br><small>un</small></div></div>
        {% endfor %}</body></html>
        """
        @app.route("/")
        def index():
            try:
                conn_flask = sqlite3.connect("loja.db", timeout=30)
                cursor_flask = conn_flask.cursor()
                cursor_flask.execute("SELECT nome, preco, estoque FROM prods ORDER BY nome")
                dados = cursor_flask.fetchall()
                conn_flask.close()
                return render_template_string(HTML_TEMPLATE, produtos=dados)
            except: return "Erro DB"

        def run_server():
            app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

        t = threading.Thread(target=run_server, daemon=True)
        t.start()

    def mostrar_info_celular(self):
        if not TEM_FLASK: return messagebox.showwarning("Aviso", "Instale o flask: pip install flask")
        messagebox.showinfo("Acesso Celular", f"Acesse no navegador do celular:\nhttp://{self.ip_local}:5000")

    # --- IMPRESSÃO (TÉRMICA) ---
    def enviar_para_impressora(self, texto):
        if not TEM_IMPRESSORA:
            try:
                arquivo_tmp = tempfile.mktemp(".txt")
                with open(arquivo_tmp, "w", encoding="utf-8") as f: f.write(texto)
                os.startfile(arquivo_tmp)
            except: pass
            return

        try:
            impressora_padrao = win32print.GetDefaultPrinter()
            hPrinter = win32print.OpenPrinter(impressora_padrao)
            try:
                hJob = win32print.StartDocPrinter(hPrinter, 1, ("Sistema Dois Irmaos", None, "RAW"))
                try:
                    win32print.StartPagePrinter(hPrinter)
                    raw_data = bytes(texto, "latin-1", errors="replace")
                    win32print.WritePrinter(hPrinter, raw_data)
                    win32print.WritePrinter(hPrinter, b'\n\n\n\n\x1d\x56\x00') # Corte
                    win32print.EndPagePrinter(hPrinter)
                finally:
                    win32print.EndDocPrinter(hPrinter)
            finally:
                win32print.ClosePrinter(hPrinter)
        except Exception as e:
            messagebox.showerror("Erro Impressão", f"Falha: {str(e)}")

    def gerar_layout_cupom(self, id_venda, cliente, endereco_cliente, carrinho, subtotal, taxa, total, pgto, valor_recebido=0.0):
        data_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
        LARGURA = 48 
        traco = "=" * LARGURA
        
        txt = "\n" + "DOIS IRMAOS - AGUA E GAS".center(LARGURA) + "\n"
        txt += f"{traco}\nVENDA #{id_venda:04d} - {data_hora}\n"
        
        prefixo_cli = "Cli: "
        for i, l in enumerate(textwrap.wrap(cliente, width=LARGURA - len(prefixo_cli))):
            txt += f"{prefixo_cli if i==0 else ' '*len(prefixo_cli)}{l}\n"

        if endereco_cliente:
            prefixo_end = "End: "
            for i, l in enumerate(textwrap.wrap(endereco_cliente, width=LARGURA - len(prefixo_end))):
                txt += f"{prefixo_end if i==0 else ' '*len(prefixo_end)}{l}\n"

        txt += f"{traco}\n{'PRODUTO':<28} {'QTD':^5} {'TOTAL':>13}\n"
        for item in carrinho:
            nome_prod = item['n']
            if len(nome_prod) > 28: nome_prod = nome_prod[:28]
            txt += f"{nome_prod:<28} {int(item['q']):^5} {item['t']:>13.2f}\n"
            
        txt += f"{traco}\nTOTAL A PAGAR: {total:>33.2f}\nPagamento: {pgto}\n"
        if pgto == "Dinheiro" and valor_recebido > 0:
            txt += f"TROCO: {(valor_recebido - total):>41.2f}\n"
        txt += "\n" + "OBRIGADO PELA PREFERENCIA!".center(LARGURA) + "\n\n\n"
        return txt

    # --- NOVO MÉTODO AUXILIAR PARA CALCULAR O TOTAL DE ITENS VENDIDOS ---
    def calcular_produtos_vendidos_hoje(self):
        hoje = datetime.now().strftime("%d/%m/%Y")
        
        with self.get_db_connection() as conn:
            cursor = conn.cursor()
            # Pega apenas os itens vendidos hoje
            cursor.execute("SELECT itens_json FROM historico WHERE data LIKE ?", (f"{hoje}%",))
            dados_brutos = cursor.fetchall()

        resumo_produtos = {} # Dicionário: {'NOME PRODUTO': Quantidade}

        for (json_str,) in dados_brutos:
            try:
                lista_itens = json.loads(json_str)
                for item in lista_itens:
                    nome = item['n']
                    qtd = float(item['q'])
                    
                    if nome in resumo_produtos:
                        resumo_produtos[nome] += qtd
                    else:
                        resumo_produtos[nome] = qtd
            except:
                pass
        
        # Retorna uma lista ordenada pelo nome do produto
        # Exemplo: [('AGUA 20L', 50.0), ('GAS P13', 10.0)]
        return sorted(resumo_produtos.items())

    # --- FUNÇÃO DE EXPORTAÇÃO PDF (ATUALIZADA) ---
    def exportar_relatorio_pdf_geral(self):
        if not TEM_REPORTLAB:
            return messagebox.showerror("Erro", "Instale a biblioteca: pip install reportlab")

        hoje = datetime.now().strftime("%d/%m/%Y")
        
        # 1. Dados Financeiros
        with self.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT forma, COUNT(*), SUM(total) FROM historico WHERE data LIKE ? GROUP BY forma", (f"{hoje}%",))
            dados_vendas = cursor.fetchall()

        # 2. Dados de Produtos Vendidos (Nova Lógica)
        dados_itens_vendidos = self.calcular_produtos_vendidos_hoje()

        # 3. Configuração do PDF
        filename = filedialog.asksaveasfilename(defaultextension=".pdf", 
                                                filetypes=[("PDF files", "*.pdf")],
                                                initialfile=f"Relatorio_Vendas_{datetime.now().strftime('%d-%m-%Y')}.pdf")
        if not filename: return

        try:
            doc = SimpleDocTemplate(filename, pagesize=A4)
            elements = []
            styles = getSampleStyleSheet()
            
            # Título
            elements.append(Paragraph(f"Relatório de Vendas - {hoje}", styles['Title']))
            elements.append(Paragraph("Sistema Dois Irmãos", styles['Normal']))
            elements.append(Spacer(1, 20))

            # --- SEÇÃO 1: FINANCEIRO ---
            elements.append(Paragraph("<b>RESUMO FINANCEIRO</b>", styles['Heading2']))
            elements.append(Spacer(1, 10))

            data_vendas = [['Forma Pagamento', 'Qtd Vendas', 'Valor Total (R$)']]
            total_geral = 0.0
            qtd_geral = 0
            
            for forma, qtd, valor in dados_vendas:
                if forma:
                    data_vendas.append([forma, str(qtd), f"R$ {valor:.2f}"])
                    total_geral += valor
                    qtd_geral += qtd
            
            data_vendas.append(['TOTAL GERAL', str(qtd_geral), f"R$ {total_geral:.2f}"])

            t_vendas = Table(data_vendas, colWidths=[200, 100, 150])
            t_vendas.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('BACKGROUND', (0, -1), (-1, -1), colors.lightgrey),
            ]))
            elements.append(t_vendas)
            elements.append(Spacer(1, 30))

            # --- SEÇÃO 2: QUANTIDADE DE ITENS VENDIDOS (ALTERADO) ---
            elements.append(Paragraph("<b>RESUMO DE ITENS VENDIDOS (SAÍDAS)</b>", styles['Heading2']))
            elements.append(Spacer(1, 10))

            data_itens = [['Produto', 'Qtd Vendida']]
            total_itens_vendidos = 0

            for nome, qtd in dados_itens_vendidos:
                qtd_fmt = f"{qtd:.0f}" if qtd.is_integer() else f"{qtd:.2f}"
                data_itens.append([nome, qtd_fmt])
                total_itens_vendidos += qtd

            # Linha de total de itens
            data_itens.append(['TOTAL DE VOLUMES/ITENS', f"{total_itens_vendidos:.0f}"])

            t_itens = Table(data_itens, colWidths=[350, 100])
            t_itens.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.darkblue),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (0, -1), 'LEFT'),
                ('ALIGN', (1, 0), (1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ROWBACKGROUNDS', (0, 1), (-2, -1), [colors.whitesmoke, colors.lightgrey]),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'), # Negrito no total
                ('BACKGROUND', (0, -1), (-1, -1), colors.lightyellow),
            ]))
            elements.append(t_itens)

            doc.build(elements)
            messagebox.showinfo("Sucesso", f"PDF salvo em:\n{filename}")
            try: os.startfile(filename)
            except: pass

        except Exception as e:
            messagebox.showerror("Erro PDF", f"Falha ao criar PDF: {str(e)}")

    # --- AUTOCOMPLETE ---
    def fechar_sugestoes(self):
        if self.lista_sugestoes: self.lista_sugestoes.destroy(); self.lista_sugestoes = None

    def verificar_foco_sugestao(self, event):
        if self.lista_sugestoes: self.fechar_sugestoes()

    def selecionar_sugestao(self, campo, valor, tabela):
        campo.delete(0, 'end'); campo.insert(0, valor); self.fechar_sugestoes()
        if campo == self.ent_cli: self.buscar_endereco_cliente()
        if campo == self.ent_prod_venda: self.ent_qtd.focus()

    def mostrar_autocomplete(self, event, campo, tabela):
        if event.keysym in ["Up", "Down", "Return", "Escape", "Tab"]: return
        texto = campo.get().upper().strip()
        if not texto: self.fechar_sugestoes(); return
        
        def buscar():
            try:
                with self.get_db_connection() as conn:
                    cursor = conn.cursor()
                    cursor.execute(f"SELECT nome FROM {tabela} WHERE nome LIKE ? LIMIT 6", (f'%{texto}%',))
                    sugestoes = [r[0] for r in cursor.fetchall()]
                
                if sugestoes:
                    self.after(0, lambda: self._desenhar_sugestoes(campo, sugestoes, tabela))
                else:
                    self.after(0, self.fechar_sugestoes)
            except: pass
            
        threading.Thread(target=buscar, daemon=True).start()
    
    def _desenhar_sugestoes(self, campo, sugestoes, tabela):
        if not self.lista_sugestoes:
            self.lista_sugestoes = ctk.CTkToplevel(self)
            self.lista_sugestoes.overrideredirect(True)
            self.lista_sugestoes.transient(self); self.lista_sugestoes.lift()
        
        try:
            x = campo.winfo_rootx(); y = campo.winfo_rooty() + campo.winfo_height()
            self.lista_sugestoes.geometry(f"{campo.winfo_width()}x{len(sugestoes)*35}+{x}+{y}")
            for w in self.lista_sugestoes.winfo_children(): w.destroy()
            for nome in sugestoes:
                ctk.CTkButton(self.lista_sugestoes, text=nome, anchor="w", fg_color="#333", hover_color="#555",
                            command=lambda n=nome: self.selecionar_sugestao(campo, n, tabela)).pack(fill="x")
        except: self.fechar_sugestoes()

    # --- LAYOUTS ---
    def montar_aba_venda(self):
        self.tab_venda.grid_columnconfigure(0, weight=1); self.tab_venda.grid_columnconfigure(1, weight=1); self.tab_venda.grid_rowconfigure(0, weight=1)

        # ESQUERDA
        self.f_esq = ctk.CTkFrame(self.tab_venda)
        self.f_esq.grid(row=0, column=0, padx=10, pady=10, sticky="nsew")
        
        ctk.CTkButton(self.f_esq, text="📱 IP CELULAR", fg_color="#673AB7", command=self.mostrar_info_celular).pack(fill="x", padx=10, pady=5)
        
        self.lbl_modo_edicao = ctk.CTkLabel(self.f_esq, text="MODO EDIÇÃO", fg_color="red"); self.lbl_modo_edicao.pack(fill="x", pady=5); self.lbl_modo_edicao.pack_forget()
        
        ctk.CTkLabel(self.f_esq, text="CLIENTE", font=("Arial", 12, "bold")).pack(anchor="w", padx=10, pady=(5,0))
        self.ent_cli = ctk.CTkEntry(self.f_esq, placeholder_text="Nome..."); self.ent_cli.pack(fill="x", padx=10, pady=5)
        self.ent_cli.bind("<KeyRelease>", lambda e: self.mostrar_autocomplete(e, self.ent_cli, "clientes"))
        
        self.ent_end = ctk.CTkEntry(self.f_esq, placeholder_text="Endereço..."); self.ent_end.pack(fill="x", padx=10, pady=5)
        
        ctk.CTkLabel(self.f_esq, text="PRODUTO", font=("Arial", 12, "bold")).pack(anchor="w", padx=10, pady=(10,0))
        self.ent_prod_venda = ctk.CTkEntry(self.f_esq, placeholder_text="Buscar..."); self.ent_prod_venda.pack(fill="x", padx=10, pady=5)
        self.ent_prod_venda.bind("<KeyRelease>", lambda e: self.mostrar_autocomplete(e, self.ent_prod_venda, "prods"))
        self.ent_prod_venda.bind("<Return>", lambda e: self.add_item())

        f_qtd = ctk.CTkFrame(self.f_esq, fg_color="transparent"); f_qtd.pack(fill="x", padx=10, pady=5)
        self.ent_qtd = ctk.CTkEntry(f_qtd, width=60); self.ent_qtd.pack(side="left"); self.ent_qtd.insert(0, "1")
        self.ent_qtd.bind("<Return>", lambda e: self.add_item())
        ctk.CTkButton(f_qtd, text="+ ADICIONAR", width=120, fg_color="green", command=self.add_item).pack(side="right", fill="x", expand=True, padx=(10,0))

        ctk.CTkLabel(self.f_esq, text="PAGAMENTO", font=("Arial", 12, "bold")).pack(anchor="w", padx=10, pady=(15,0))
        self.forma_pagamento = ctk.CTkOptionMenu(self.f_esq, values=["Dinheiro", "Cartão", "PIX", "Link", "Outros"], command=lambda e: self.atualizar_totais())
        self.forma_pagamento.pack(fill="x", padx=10, pady=5)
        self.ent_taxa_entrega = ctk.CTkEntry(self.f_esq, placeholder_text="Taxa Entrega"); self.ent_taxa_entrega.pack(fill="x", padx=10, pady=5)
        self.ent_valor_pago = ctk.CTkEntry(self.f_esq, placeholder_text="Valor Recebido"); self.ent_valor_pago.pack(fill="x", padx=10, pady=5)

        # DIREITA
        self.f_dir = ctk.CTkFrame(self.tab_venda)
        self.f_dir.grid(row=0, column=1, padx=10, pady=10, sticky="nsew")
        
        ctk.CTkLabel(self.f_dir, text="ITENS DA VENDA (Duplo clique para editar)", text_color="gray").pack(pady=(5,0))
        
        f_carrinho = ctk.CTkFrame(self.f_dir, fg_color="transparent")
        f_carrinho.pack(fill="both", expand=True, padx=5, pady=5)

        self.cols_carrinho = ("ITEM", "QTD", "UNIT", "TOTAL")
        self.tree_carrinho = ttk.Treeview(f_carrinho, columns=self.cols_carrinho, show="headings", selectmode="browse")
        
        self.tree_carrinho.heading("ITEM", text="PRODUTO"); self.tree_carrinho.column("ITEM", width=200, anchor="w")
        self.tree_carrinho.heading("QTD", text="QTD"); self.tree_carrinho.column("QTD", width=50, anchor="center")
        self.tree_carrinho.heading("UNIT", text="UNIT"); self.tree_carrinho.column("UNIT", width=80, anchor="e")
        self.tree_carrinho.heading("TOTAL", text="TOTAL"); self.tree_carrinho.column("TOTAL", width=80, anchor="e")

        vsb_car = ttk.Scrollbar(f_carrinho, orient="vertical", command=self.tree_carrinho.yview)
        self.tree_carrinho.configure(yscrollcommand=vsb_car.set)
        self.tree_carrinho.pack(side="left", fill="both", expand=True)
        vsb_car.pack(side="right", fill="y")
        
        self.tree_carrinho.bind("<Double-1>", self.editar_item_carrinho)
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
        self.btn_salvar_cli = ctk.CTkButton(f, text="SALVAR", fg_color="green", width=100, command=self.salvar_cliente)
        self.btn_salvar_cli.pack(side="left", padx=5)
        self.btn_cancelar_cli = ctk.CTkButton(f, text="X", width=30, fg_color="red", command=self.cancelar_edicao_cli); self.btn_cancelar_cli.pack(side="left", padx=2); self.btn_cancelar_cli.pack_forget()

        f_acoes = ctk.CTkFrame(self.tab_clientes, fg_color="transparent"); f_acoes.pack(fill="x", padx=10)
        ctk.CTkButton(f_acoes, text="EDITAR SELECIONADO", fg_color="#007bff", command=self.carregar_cliente_para_edicao).pack(side="left", padx=5)
        ctk.CTkButton(f_acoes, text="EXCLUIR SELECIONADO(S)", fg_color="red", command=self.excluir_clientes_selecionados).pack(side="left", padx=5)
        
        self.ent_busca_cli = ctk.CTkEntry(self.tab_clientes, placeholder_text="Buscar Cliente..."); self.ent_busca_cli.pack(fill="x", padx=10, pady=5)
        self.ent_busca_cli.bind("<KeyRelease>", lambda e: self.atualizar_lista_clientes())
        
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
        
        self.atualizar_lista_clientes()

    def montar_aba_produtos(self):
        f_topo = ctk.CTkFrame(self.tab_produtos); f_topo.pack(fill="x", padx=10, pady=10)
        ctk.CTkLabel(f_topo, text="CADASTRO DE PRODUTO", font=("Arial", 12, "bold")).pack(anchor="w", padx=5)
        f_cad = ctk.CTkFrame(f_topo, fg_color="transparent"); f_cad.pack(fill="x", pady=2)
        self.ent_p_nome = ctk.CTkEntry(f_cad, placeholder_text="Nome Produto"); self.ent_p_nome.pack(side="left", expand=True, fill="x", padx=5)
        self.ent_p_preco = ctk.CTkEntry(f_cad, placeholder_text="Preço R$", width=100); self.ent_p_preco.pack(side="left", padx=5)
        self.btn_salvar_prod = ctk.CTkButton(f_cad, text="SALVAR / CRIAR", fg_color="green", width=120, command=self.salvar_produto); self.btn_salvar_prod.pack(side="left", padx=5)
        self.btn_cancelar_prod = ctk.CTkButton(f_cad, text="X", width=30, fg_color="red", command=self.cancelar_edicao_prod); self.btn_cancelar_prod.pack(side="left", padx=2); self.btn_cancelar_prod.pack_forget()
        
        f_entrada = ctk.CTkFrame(self.tab_produtos, border_color="#FF9800", border_width=2); f_entrada.pack(fill="x", padx=10, pady=10)
        ctk.CTkLabel(f_entrada, text="🚚 CHEGADA DE MERCADORIA", font=("Arial", 12, "bold"), text_color="#FF9800").pack(anchor="w", padx=10, pady=5)
        f_ent_cont = ctk.CTkFrame(f_entrada, fg_color="transparent"); f_ent_cont.pack(fill="x", padx=5, pady=5)
        self.ent_entrada_busca = ctk.CTkEntry(f_ent_cont, placeholder_text="Busque produto..."); self.ent_entrada_busca.pack(side="left", fill="x", expand=True, padx=5)
        self.ent_entrada_busca.bind("<KeyRelease>", lambda e: self.mostrar_autocomplete(e, self.ent_entrada_busca, "prods"))
        self.ent_entrada_qtd = ctk.CTkEntry(f_ent_cont, placeholder_text="Qtd Chegou", width=100); self.ent_entrada_qtd.pack(side="left", padx=5)
        ctk.CTkButton(f_ent_cont, text="➕ REGISTRAR", fg_color="#FF9800", text_color="black", command=self.dar_entrada_estoque).pack(side="left", padx=5)

        f_busca = ctk.CTkFrame(self.tab_produtos); f_busca.pack(fill="x", padx=10, pady=5)
        self.ent_busca_prod = ctk.CTkEntry(f_busca, placeholder_text="🔍 Filtrar tabela..."); self.ent_busca_prod.pack(side="left", fill="x", expand=True, padx=5)
        self.ent_busca_prod.bind("<KeyRelease>", lambda e: self.atualizar_lista_produtos())
        
        ctk.CTkButton(f_busca, text="✏ EDITAR", width=100, fg_color="#007bff", command=self.carregar_produto_para_edicao).pack(side="left", padx=5)
        ctk.CTkButton(f_busca, text="🗑 EXCLUIR", width=100, fg_color="red", command=self.excluir_produtos_selecionados).pack(side="left", padx=5)
        
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
        
        self.atualizar_lista_produtos()

    def montar_aba_relatorio(self):
        f = ctk.CTkFrame(self.tab_relatorio); f.pack(fill="x", padx=10, pady=10)
        self.ent_id_rel = ctk.CTkEntry(f, placeholder_text="ID", width=60)
        self.ent_id_rel.pack(side="left", padx=5)
        self.ent_id_rel.bind("<KeyRelease>", lambda e: self.atualizar_lista_vendas())

        self.ent_data_rel = ctk.CTkEntry(f, placeholder_text="Data (DD/MM/YYYY)", width=140)
        self.ent_data_rel.pack(side="left", padx=5)
        self.ent_data_rel.bind("<KeyRelease>", lambda e: self.atualizar_lista_vendas())

        self.ent_cli_rel = ctk.CTkEntry(f, placeholder_text="Filtrar por Nome do Cliente...", width=200)
        self.ent_cli_rel.pack(side="left", fill="x", expand=True, padx=5)
        self.ent_cli_rel.bind("<KeyRelease>", lambda e: self.atualizar_lista_vendas())

        # BOTOES UNIFICADOS
        ctk.CTkButton(f, text="🖨 IMPRIMIR GERAL", width=140, fg_color="orange", command=self.relatorio_geral_dia).pack(side="right", padx=5)
        ctk.CTkButton(f, text="📄 SALVAR PDF", width=140, fg_color="#673AB7", command=self.exportar_relatorio_pdf_geral).pack(side="right", padx=5)
        
        f_acoes = ctk.CTkFrame(self.tab_relatorio, fg_color="transparent"); f_acoes.pack(fill="x", padx=10)
        ctk.CTkButton(f_acoes, text="REIMPRIMIR CUPOM", fg_color="grey", command=self.reimprimir_cupom).pack(side="left", padx=5)
        ctk.CTkButton(f_acoes, text="EDITAR VENDA", fg_color="purple", command=self.editar_venda_antiga).pack(side="left", padx=5)
        ctk.CTkButton(f_acoes, text="EXCLUIR", fg_color="red", command=self.excluir_vendas_selecionadas).pack(side="left", padx=5)
        
        f_tree = ctk.CTkFrame(self.tab_relatorio, fg_color="transparent")
        f_tree.pack(fill="both", expand=True, padx=10, pady=5)
        self.cols_rel = ("ID", "DATA", "CLIENTE", "TOTAL", "FORMA")
        self.tree_rel = ttk.Treeview(f_tree, columns=self.cols_rel, show="headings", selectmode="extended")
        self.tree_rel.heading("ID", text="ID"); self.tree_rel.column("ID", width=60, anchor="center")
        self.tree_rel.heading("DATA", text="DATA/HORA"); self.tree_rel.column("DATA", width=150, anchor="center")
        self.tree_rel.heading("CLIENTE", text="CLIENTE"); self.tree_rel.column("CLIENTE", width=400, anchor="w")
        self.tree_rel.heading("TOTAL", text="TOTAL R$"); self.tree_rel.column("TOTAL", width=100, anchor="e")
        self.tree_rel.heading("FORMA", text="PAGAMENTO"); self.tree_rel.column("FORMA", width=150, anchor="center")
        
        vsb = ttk.Scrollbar(f_tree, orient="vertical", command=self.tree_rel.yview)
        self.tree_rel.configure(yscrollcommand=vsb.set)
        self.tree_rel.pack(side="left", fill="both", expand=True); vsb.pack(side="right", fill="y")
        
        self.atualizar_lista_vendas()

    # --- LÓGICA DO CARRINHO ---
    def renderizar_carrinho(self):
        for i in self.tree_carrinho.get_children():
            self.tree_carrinho.delete(i)
        
        self.total_itens = 0.0
        
        for i, item in enumerate(self.carrinho):
            if 'vu' not in item:
                item['vu'] = item['t'] / item['q'] if item['q'] > 0 else 0
                
            valor_formatado = f"R$ {item['vu']:.2f}"
            total_formatado = f"R$ {item['t']:.2f}"
            qtd_formatada = f"{item['q']:.0f}" if float(item['q']).is_integer() else f"{item['q']}"
            
            self.tree_carrinho.insert("", "end", iid=i, values=(item['n'], qtd_formatada, valor_formatado, total_formatado))
            self.total_itens += item['t']
            
        self.atualizar_totais()

    def add_item(self):
        nome = self.ent_prod_venda.get().upper()
        if not nome: return
        
        with self.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT preco, estoque FROM prods WHERE nome=?", (nome,))
            res = cursor.fetchone()
        
        if res:
            q_solicitada = self.safe_float(self.ent_qtd.get()) or 1
            estoque_atual = res[1]
            if estoque_atual < q_solicitada:
                if not messagebox.askyesno("Estoque Baixo", f"ATENÇÃO: Só há {estoque_atual} un.\nVender mesmo assim?"): return

            sub = q_solicitada * res[0]
            self.carrinho.append({"n": nome, "q": q_solicitada, "t": sub, "vu": res[0]})
            self.renderizar_carrinho()
            
            self.ent_prod_venda.delete(0, 'end'); self.ent_qtd.delete(0, 'end'); self.ent_qtd.insert(0, "1"); self.ent_prod_venda.focus()
        else:
            messagebox.showwarning("Erro", "Produto não encontrado")

    def editar_item_carrinho(self, event):
        selecao = self.tree_carrinho.selection()
        if not selecao: return
        
        idx = int(selecao[0])
        item = self.carrinho[idx]
        
        novo = ctk.CTkInputDialog(text=f"Nova quantidade para {item['n']}:", title="Editar").get_input()
        if novo:
            q = self.safe_float(novo)
            if q <= 0:
                self.carrinho.pop(idx)
            else:
                item['q'] = q
                item['t'] = q * item['vu']
            self.renderizar_carrinho()

    def excluir_item_carrinho(self, event):
        selecao = self.tree_carrinho.selection()
        if not selecao: return
        idx = int(selecao[0])
        self.carrinho.pop(idx)
        self.renderizar_carrinho()

    def atualizar_totais(self):
        tx = self.safe_float(self.ent_taxa_entrega.get())
        pg = self.safe_float(self.ent_valor_pago.get())
        tot = self.total_itens + tx
        if self.forma_pagamento.get() == "Dinheiro" and pg > 0:
            self.lbl_troco.configure(text=f"TROCO: R$ {pg - tot:.2f}")
        else:
            self.lbl_troco.configure(text="TROCO: --")
        self.lbl_total.configure(text=f"TOTAL: R$ {tot:.2f}")

    def finalizar_venda(self, imprimir=True):
        if not self.carrinho: return messagebox.showwarning("Vazio", "Carrinho vazio!")
        
        cli = self.ent_cli.get().upper() or "CONSUMIDOR"
        end = self.ent_end.get().upper()
        pgto = self.forma_pagamento.get()
        tx = self.safe_float(self.ent_taxa_entrega.get())
        total = self.total_itens + tx
        val_pago = self.safe_float(self.ent_valor_pago.get())
        data_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
        
        try:
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                
                if cli != "CONSUMIDOR":
                    cursor.execute("SELECT id FROM clientes WHERE nome=?", (cli,))
                    if not cursor.fetchone():
                        cursor.execute("INSERT INTO clientes (nome, endereco) VALUES (?,?)", (cli, end))

                for item in self.carrinho:
                    cursor.execute("UPDATE prods SET estoque = estoque - ? WHERE nome = ?", (item['q'], item['n']))

                if self.id_venda_em_edicao:
                    cursor.execute("UPDATE historico SET data=?, cliente=?, total=?, forma=?, taxa=?, itens_json=? WHERE id=?",
                                   (data_hora, cli, total, pgto, tx, json.dumps(self.carrinho), self.id_venda_em_edicao))
                    self.id_venda_em_edicao = None
                    self.lbl_modo_edicao.pack_forget()
                    self.btn_salvar_apenas.configure(text="SALVAR (F4)")
                    self.btn_finalizar.configure(text="IMPRIMIR (F5)", fg_color="#2196F3")
                    venda_id = self.id_venda_em_edicao
                else:
                    cursor.execute("INSERT INTO historico (data, cliente, total, forma, taxa, itens_json) VALUES (?,?,?,?,?,?)",
                                   (data_hora, cli, total, pgto, tx, json.dumps(self.carrinho)))
                    venda_id = cursor.lastrowid
                
                conn.commit()
            
            if imprimir:
                cupom = self.gerar_layout_cupom(venda_id if venda_id else 0, cli, end, self.carrinho, self.total_itens, tx, total, pgto, val_pago)
                self.enviar_para_impressora(cupom)
            
            self.carrinho = []
            self.renderizar_carrinho()
            self.ent_cli.delete(0, 'end'); self.ent_end.delete(0, 'end')
            self.ent_taxa_entrega.delete(0, 'end'); self.ent_valor_pago.delete(0, 'end')
            self.atualizar_totais(); self.atualizar_lista_vendas(); self.atualizar_lista_produtos()
            
            msg = "Venda Salva e Impressa!" if imprimir else "Venda Salva (Sem Imprimir)!"
            messagebox.showinfo("Sucesso", msg)
        except sqlite3.Error as e:
            messagebox.showerror("Erro", str(e))

    # --- GERENCIAMENTO PRODUTOS ---
    def salvar_produto(self):
        nome = self.ent_p_nome.get().upper()
        preco = self.safe_float(self.ent_p_preco.get())
        if not nome: return
        
        with self.get_db_connection() as conn:
            cursor = conn.cursor()
            if self.id_prod_em_edicao:
                cursor.execute("UPDATE prods SET nome=?, preco=? WHERE id=?", (nome, preco, self.id_prod_em_edicao))
                self.cancelar_edicao_prod()
            else:
                cursor.execute("INSERT INTO prods (nome, preco, estoque) VALUES (?,?,0)", (nome, preco))
            conn.commit()
        
        self.atualizar_lista_produtos()
        self.ent_p_nome.delete(0,'end'); self.ent_p_preco.delete(0,'end')

    def dar_entrada_estoque(self):
        nome = self.ent_entrada_busca.get().upper()
        qtd = self.safe_int(self.ent_entrada_qtd.get())
        data_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
        if not nome or qtd <= 0: return messagebox.showwarning("Erro", "Selecione e digite qtd.")
        
        with self.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, estoque FROM prods WHERE nome=?", (nome,))
            res = cursor.fetchone()
            if res:
                novo_estoque = res[1] + qtd
                cursor.execute("UPDATE prods SET estoque=? WHERE id=?", (novo_estoque, res[0]))
                cursor.execute("INSERT INTO entradas_estoque (nome_prod, qtd, data) VALUES (?,?,?)", (nome, qtd, data_hora))
                conn.commit()
                messagebox.showinfo("Sucesso", f"Add {qtd}un ao estoque de {nome}.\nTotal: {novo_estoque}")
                self.ent_entrada_busca.delete(0, 'end'); self.ent_entrada_qtd.delete(0, 'end')
                self.atualizar_lista_produtos()
            else:
                messagebox.showerror("Erro", "Produto não cadastrado.")

    def calcular_movimentacoes_hoje(self):
        hoje = datetime.now().strftime("%d/%m/%Y")
        with self.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT itens_json FROM historico WHERE data LIKE ?", (f"{hoje}%",))
            vendas = cursor.fetchall()
        
        dados = {} 
        for (json_str,) in vendas:
            try:
                itens = json.loads(json_str)
                for item in itens:
                    nome = item['n']; qtd = item['q']
                    if nome not in dados: dados[nome] = {'saiu':0, 'entrou':0}
                    dados[nome]['saiu'] += qtd
            except: pass
        return dados

    # =========================================================================
    # THREADS DE BUSCA
    # =========================================================================

    def atualizar_lista_produtos(self):
        for i in self.tree_prod.get_children(): self.tree_prod.delete(i)
        termo = self.ent_busca_prod.get().upper()
        t = threading.Thread(target=self._thread_buscar_produtos, args=(termo,))
        t.daemon = True; t.start()

    def _thread_buscar_produtos(self, termo):
        try:
            with self.get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id, nome, preco, estoque FROM prods WHERE nome LIKE ? ORDER BY nome LIMIT 50", (f'%{termo}%',))
                resultados = cur.fetchall()
            self.after(0, self._callback_desenhar_produtos, resultados)
        except Exception as e: print(f"Erro Thread Prod: {e}")

    def _callback_desenhar_produtos(self, resultados):
        movs = self.calcular_movimentacoes_hoje()
        for pid, nome, preco, estoque in resultados:
            qtd_saiu = movs.get(nome, {}).get('saiu', 0)
            tag = "baixo" if estoque < 10 else "normal"
            self.tree_prod.insert("", "end", values=(pid, nome, f"R$ {preco:.2f}", estoque, int(qtd_saiu)), tags=(tag,))

    def atualizar_lista_clientes(self):
        for i in self.tree_cli.get_children(): self.tree_cli.delete(i)
        termo = self.ent_busca_cli.get().upper()
        t = threading.Thread(target=self._thread_buscar_clientes, args=(termo,))
        t.daemon = True; t.start()

    def _thread_buscar_clientes(self, termo):
        try:
            with self.get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT id, nome, endereco FROM clientes WHERE nome LIKE ? ORDER BY nome LIMIT 50", (f'%{termo}%',))
                resultados = cur.fetchall()
            self.after(0, self._callback_desenhar_clientes, resultados)
        except Exception as e: print(f"Erro Thread Cli: {e}")

    def _callback_desenhar_clientes(self, resultados):
        for cid, nome, end in resultados:
            self.tree_cli.insert("", "end", values=(cid, nome, end))

    def atualizar_lista_vendas(self):
        for i in self.tree_rel.get_children(): self.tree_rel.delete(i)
        f_id = self.ent_id_rel.get().strip()
        f_data = self.ent_data_rel.get().strip()
        f_cli = self.ent_cli_rel.get().strip().upper()
        t = threading.Thread(target=self._thread_buscar_vendas, args=(f_id, f_data, f_cli))
        t.daemon = True; t.start()

    def _thread_buscar_vendas(self, f_id, f_data, f_cli):
        try:
            with self.get_db_connection() as conn:
                cur = conn.cursor()
                query = "SELECT id, data, cliente, total, forma FROM historico WHERE 1=1"
                params = []
                if f_id: query += " AND id = ?"; params.append(f_id)
                if f_data: query += " AND data LIKE ?"; params.append(f"%{f_data}%")
                if f_cli: query += " AND cliente LIKE ?"; params.append(f"%{f_cli}%")
                query += " ORDER BY id DESC LIMIT 50"
                cur.execute(query, params)
                resultados = cur.fetchall()
            self.after(0, self._callback_desenhar_vendas, resultados)
        except Exception as e: print(f"Erro Thread Vendas: {e}")

    def _callback_desenhar_vendas(self, resultados):
        for vid, d, c, t, f in resultados:
            self.tree_rel.insert("", "end", values=(vid, d, c, f"R$ {t:.2f}", f))

    def excluir_produtos_selecionados(self):
        selected = self.tree_prod.selection()
        if not selected: return
        ids = [self.tree_prod.item(i, "values")[0] for i in selected]
        if messagebox.askyesno("Confirmar", f"Excluir {len(ids)} produtos?"):
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"DELETE FROM prods WHERE id IN ({','.join('?'*len(ids))})", ids)
                conn.commit()
            self.atualizar_lista_produtos()

    def carregar_produto_para_edicao(self):
        selected = self.tree_prod.selection()
        if len(selected) != 1: return messagebox.showwarning("Erro", "Selecione apenas 1 produto")
        val = self.tree_prod.item(selected[0], "values")
        pid, nome, preco_str = val[0], val[1], val[2]
        preco = preco_str.replace("R$ ","")
        self.ent_p_nome.delete(0,'end'); self.ent_p_nome.insert(0, nome)
        self.ent_p_preco.delete(0,'end'); self.ent_p_preco.insert(0, preco)
        self.id_prod_em_edicao = pid
        self.btn_salvar_prod.configure(text="SALVAR ALTERAÇÕES", fg_color="blue")
        self.btn_cancelar_prod.pack(side="left", padx=2)

    def cancelar_edicao_prod(self):
        self.id_prod_em_edicao = None
        self.ent_p_nome.delete(0,'end'); self.ent_p_preco.delete(0,'end')
        self.btn_salvar_prod.configure(text="SALVAR / CRIAR", fg_color="green")
        self.btn_cancelar_prod.pack_forget()

    def salvar_cliente(self):
        nome = self.ent_c_nome.get().upper(); end = self.ent_c_end.get().upper()
        if not nome: return
        with self.get_db_connection() as conn:
            cursor = conn.cursor()
            if self.id_cliente_em_edicao:
                cursor.execute("UPDATE clientes SET nome=?, endereco=? WHERE id=?", (nome, end, self.id_cliente_em_edicao))
                self.cancelar_edicao_cli()
            else:
                cursor.execute("INSERT INTO clientes (nome, endereco) VALUES (?,?)", (nome, end))
            conn.commit()
        
        self.atualizar_lista_clientes(); self.ent_c_nome.delete(0,'end'); self.ent_c_end.delete(0,'end')

    def buscar_endereco_cliente(self):
        with self.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT endereco FROM clientes WHERE nome=?", (self.ent_cli.get().upper(),))
            r = cursor.fetchone()
        if r: self.ent_end.delete(0, 'end'); self.ent_end.insert(0, r[0])

    def excluir_clientes_selecionados(self):
        selected = self.tree_cli.selection()
        if not selected: return
        ids = [self.tree_cli.item(i, "values")[0] for i in selected]
        if messagebox.askyesno("Confirmar", f"Excluir {len(ids)} clientes?"):
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"DELETE FROM clientes WHERE id IN ({','.join('?'*len(ids))})", ids)
                conn.commit()
            self.atualizar_lista_clientes()

    def carregar_cliente_para_edicao(self):
        selected = self.tree_cli.selection()
        if len(selected) != 1: return
        val = self.tree_cli.item(selected[0], "values")
        self.ent_c_nome.delete(0,'end'); self.ent_c_nome.insert(0, val[1])
        self.ent_c_end.delete(0,'end'); self.ent_c_end.insert(0, val[2])
        self.id_cliente_em_edicao = val[0]
        self.btn_salvar_cli.configure(text="SALVAR ALTERAÇÕES", fg_color="blue")
        self.btn_cancelar_cli.pack(side="left", padx=2)

    def cancelar_edicao_cli(self):
        self.id_cliente_em_edicao = None; self.ent_c_nome.delete(0,'end'); self.ent_c_end.delete(0,'end')
        self.btn_salvar_cli.configure(text="SALVAR", fg_color="green"); self.btn_cancelar_cli.pack_forget()

    def excluir_vendas_selecionadas(self):
        selected = self.tree_rel.selection()
        if not selected: return
        ids = [self.tree_rel.item(i, "values")[0] for i in selected]
        if messagebox.askyesno("CUIDADO", "Apagar vendas selecionadas?"):
            with self.get_db_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"DELETE FROM historico WHERE id IN ({','.join('?'*len(ids))})", ids)
                conn.commit()
            self.atualizar_lista_vendas()

    def reimprimir_cupom(self):
        selected = self.tree_rel.selection()
        if len(selected) != 1: return
        vid = self.tree_rel.item(selected[0], "values")[0]
        with self.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM historico WHERE id=?", (vid,))
            r = cursor.fetchone()
        
        carrinho = json.loads(r[6])
        cupom = self.gerar_layout_cupom(r[0], r[2], "", carrinho, r[3]-r[5], r[5], r[3], r[4])
        self.enviar_para_impressora(cupom)

    def editar_venda_antiga(self):
        selected = self.tree_rel.selection()
        if len(selected) != 1: return
        if not messagebox.askyesno("Confirmar", "Editar venda?"): return
        vid = self.tree_rel.item(selected[0], "values")[0]
        
        with self.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM historico WHERE id=?", (vid,))
            r = cursor.fetchone()
            
        self.id_venda_em_edicao = r[0]
        self.ent_cli.delete(0,'end'); self.ent_cli.insert(0, r[2])
        self.forma_pagamento.set(r[4])
        self.ent_taxa_entrega.delete(0,'end'); self.ent_taxa_entrega.insert(0, str(r[5]))
        self.carrinho = json.loads(r[6])
        
        # Renderiza usando a nova Treeview
        self.renderizar_carrinho()
        
        self.lbl_modo_edicao.pack(fill="x", pady=5)
        self.btn_salvar_apenas.configure(text="SALVAR ALTERAÇÃO (F4)")
        self.btn_finalizar.configure(text=f"SALVAR E IMPRIMIR (F5)", fg_color="red")
        self.abas.set("Venda")

    # --- RELATÓRIOS (IMPRESSÃO TÉRMICA ATUALIZADA) ---
    def relatorio_geral_dia(self):
        hoje = datetime.now().strftime("%d/%m/%Y")
        
        # 1. Busca Financeiro
        with self.get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT forma, COUNT(*), SUM(total) FROM historico WHERE data LIKE ? GROUP BY forma", (f"{hoje}%",))
            dados_vendas = cursor.fetchall()
            
        # 2. Busca Itens Vendidos (Usando a nova função)
        dados_itens_vendidos = self.calcular_produtos_vendidos_hoje()

        # 3. Monta o texto
        txt = "\nFECHAMENTO GERAL DO DIA\n" + f"DATA: {hoje}\n" + "="*48 + "\n"
        
        # Bloco Financeiro
        txt += ">>> VENDAS (FINANCEIRO)\n"
        txt += f"{'FORMA':<20} {'QTD':^6} {'VALOR':>18}\n" + "-"*48 + "\n"
        
        geral_val = 0.0; geral_qtd = 0
        for f, qtd, v in dados_vendas:
            if f:
                txt += f"{f:<20} {qtd:^6} R$ {v:>13.2f}\n"
                geral_val += v; geral_qtd += qtd
        
        txt += "="*48 + "\n"
        txt += f"TOTAL GERAL:         {geral_qtd:^6} R$ {geral_val:>13.2f}\n\n\n"
        
        # Bloco de Produtos Vendidos (Alterado)
        txt += "="*48 + "\n"
        txt += ">>> RESUMO DE ITENS VENDIDOS (SAIDAS)\n"
        txt += f"{'PRODUTO':<36} {'QTD':^10}\n" + "-"*48 + "\n"
        
        total_itens_dia = 0
        for nome, qtd in dados_itens_vendidos:
            # Formata para não mostrar casas decimais se for número inteiro
            qtd_fmt = f"{qtd:.0f}" if qtd.is_integer() else f"{qtd:.2f}"
            txt += f"{nome[:36]:<36} {qtd_fmt:^10}\n"
            total_itens_dia += qtd

        txt += "-"*48 + "\n"
        txt += f"{'TOTAL DE ITENS:':<36} {total_itens_dia:.0f}\n"
        txt += "="*48 + "\n\n\n"
        
        self.enviar_para_impressora(txt)

if __name__ == "__main__":
    app = SistemaComanda()
    app.mainloop()