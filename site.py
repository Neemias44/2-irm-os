import customtkinter as ctk
import sqlite3
import json
import os
import tempfile
import threading
import socket
from datetime import datetime
from tkinter import messagebox
from tkinter import filedialog
from tkinter import simpledialog

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
        self.title("Sistema Dois Irmãos - Estoque Mobile Pro")
        self.geometry("1280x850")
        
        # Variáveis
        self.id_cliente_em_edicao = None 
        self.check_vars_cli = {} 
        self.id_prod_em_edicao = None
        self.check_vars_prod = {}
        self.check_vars_rel = {}
        self.id_venda_em_edicao = None 
        
        self.carrinho = []
        self.total_itens = 0.0
        self.lista_sugestoes = None 
        self.ip_local = self.pegar_ip()

        self.init_db()
        
        if TEM_FLASK:
            self.iniciar_servidor_celular()

        self.bind("<Button-1>", lambda e: self.verificar_foco_sugestao(e))

        # Abas
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

    def safe_float(self, valor):
        try:
            if not valor or valor.strip() == "": return 0.0
            return float(valor.replace(",", "."))
        except (ValueError, AttributeError):
            return 0.0
            
    def safe_int(self, valor):
        try:
            if not valor or valor.strip() == "": return 0
            return int(float(valor))
        except (ValueError, AttributeError):
            return 0

    def pegar_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    def init_db(self):
        db_file = "loja.db"
        try:
            self.conn = sqlite3.connect(db_file, check_same_thread=False)
            self.cursor = self.conn.cursor()
            
            self.cursor.execute("CREATE TABLE IF NOT EXISTS prods (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, preco REAL)")
            self.cursor.execute("CREATE TABLE IF NOT EXISTS clientes (id INTEGER PRIMARY KEY AUTOINCREMENT, nome TEXT, endereco TEXT)")
            self.cursor.execute("""CREATE TABLE IF NOT EXISTS historico 
                                (id INTEGER PRIMARY KEY AUTOINCREMENT, data TEXT, cliente TEXT, total REAL, forma TEXT, taxa REAL, itens_json TEXT)""")
            
            # Tabela de Entradas de Estoque
            self.cursor.execute("""CREATE TABLE IF NOT EXISTS entradas_estoque 
                                (id INTEGER PRIMARY KEY AUTOINCREMENT, nome_prod TEXT, qtd INTEGER, data TEXT)""")

            # Migração Estoque
            try:
                self.cursor.execute("SELECT estoque FROM prods LIMIT 1")
            except sqlite3.OperationalError:
                self.cursor.execute("ALTER TABLE prods ADD COLUMN estoque INTEGER DEFAULT 0")
                self.conn.commit()
                
            self.conn.commit()
            print(f"Conectado ao banco: {db_file}")
        except sqlite3.Error as e:
            messagebox.showerror("Erro de Banco", f"Erro ao conectar: {e}")

    # --- SERVIDOR CELULAR (ATUALIZADO PARA MOSTRAR TOTAIS) ---
    def iniciar_servidor_celular(self):
        app = Flask(__name__)
        HTML_TEMPLATE = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Estoque Dois Irmãos</title>
            <meta http-equiv="refresh" content="30">
            <style>
                body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #1a1a1a; color: #fff; padding: 10px; margin: 0; }
                
                .header-box { background: linear-gradient(45deg, #0d47a1, #1976d2); padding: 15px; border-radius: 10px; margin-bottom: 20px; text-align: center; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
                .header-title { font-size: 0.9em; color: #bbdefb; text-transform: uppercase; letter-spacing: 1px; }
                .header-value { font-size: 2em; font-weight: bold; color: #fff; margin: 5px 0; }
                
                .card { background: #333; padding: 15px; margin-bottom: 12px; border-radius: 8px; border-left: 6px solid #4CAF50; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 4px rgba(0,0,0,0.2); }
                .low-stock { border-left: 6px solid #f44336; }
                
                .info-col { display: flex; flex-direction: column; }
                .prod-name { font-weight: bold; font-size: 1.1em; margin-bottom: 4px; }
                .prod-price { color: #aaa; font-size: 0.85em; }
                
                .stock-col { text-align: right; }
                .stock-qty { font-size: 1.6em; font-weight: bold; color: #fff; }
                .stock-label { font-size: 0.7em; color: #ccc; }
                .total-item-val { font-size: 0.8em; color: #FFC107; margin-top: 2px; font-weight: bold; }
            </style>
        </head>
        <body>
            <div class="header-box">
                <div class="header-title">Valor Total em Mercadoria</div>
                <div class="header-value">R$ {{ "%.2f"|format(total_loja) }}</div>
                <div style="font-size: 0.8em; color: #ddd;">Total de Itens: {{ qtd_loja }} un</div>
            </div>

            {% for p in produtos %}
            <div class="card {% if p['qtd'] < 5 %}low-stock{% endif %}">
                <div class="info-col">
                    <span class="prod-name">{{ p['nome'] }}</span>
                    <span class="prod-price">Unit: R$ {{ "%.2f"|format(p['preco']) }}</span>
                </div>
                <div class="stock-col">
                    <span class="stock-qty">{{ p['qtd'] }}</span><br>
                    <span class="stock-label">Em Estoque</span>
                    <div class="total-item-val">Total: R$ {{ "%.2f"|format(p['total']) }}</div>
                </div>
            </div>
            {% endfor %}
            
            <p style="text-align:center; color:#555; font-size:0.7em; margin-top:20px">Atualiza a cada 30s</p>
        </body>
        </html>
        """

        @app.route("/")
        def index():
            conn_flask = sqlite3.connect("loja.db")
            cursor_flask = conn_flask.cursor()
            cursor_flask.execute("SELECT nome, preco, estoque FROM prods ORDER BY nome")
            raw_dados = cursor_flask.fetchall()
            conn_flask.close()

            # Processa os dados para calcular totais
            produtos_formatados = []
            total_valor_loja = 0.0
            total_qtd_loja = 0

            for nome, preco, estoque in raw_dados:
                total_item = preco * estoque
                total_valor_loja += total_item
                total_qtd_loja += estoque
                
                produtos_formatados.append({
                    'nome': nome,
                    'preco': preco,
                    'qtd': estoque,
                    'total': total_item
                })

            return render_template_string(HTML_TEMPLATE, 
                                        produtos=produtos_formatados, 
                                        total_loja=total_valor_loja,
                                        qtd_loja=total_qtd_loja)

        def run_server():
            app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

        t = threading.Thread(target=run_server, daemon=True)
        t.start()

    def mostrar_info_celular(self):
        if not TEM_FLASK:
            return messagebox.showwarning("Aviso", "Instale o flask: pip install flask")
        msg = f"Acesse no celular pelo Wi-Fi:\n\nhttp://{self.ip_local}:5000"
        messagebox.showinfo("Acesso Celular", msg)

    # --- IMPRESSÃO ---
    def enviar_para_impressora(self, texto):
        if not TEM_IMPRESSORA:
            try:
                arquivo_tmp = tempfile.mktemp(".txt")
                with open(arquivo_tmp, "w", encoding="utf-8") as f: f.write(texto)
                os.startfile(arquivo_tmp)
            except Exception as e:
                messagebox.showerror("Erro", f"Erro arquivo: {e}")
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
                    win32print.WritePrinter(hPrinter, b'\n\n\n\n\x1d\x56\x00') 
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
        txt += f"{traco}\n"
        txt += f"VENDA #{id_venda:04d} - {data_hora}\n"
        txt += f"Cli: {cliente[:40]}\n"
        if endereco_cliente: txt += f"End: {endereco_cliente[:LARGURA]}\n"
        txt += f"{traco}\n"
        txt += f"{'PRODUTO':<28} {'QTD':^5} {'TOTAL':>13}\n"
        for item in carrinho:
            txt += f"{item['n'][:28]:<28} {int(item['q']):^5} {item['t']:>13.2f}\n"
        txt += f"{traco}\n"
        txt += f"TOTAL A PAGAR: {total:>33.2f}\n"
        txt += f"Pagamento: {pgto}\n"
        if pgto == "Dinheiro" and valor_recebido > 0:
            txt += f"TROCO: {(valor_recebido - total):>41.2f}\n"
        txt += "\n" + "OBRIGADO PELA PREFERENCIA!".center(LARGURA) + "\n\n\n"
        return txt

    def salvar_arquivo_generico(self, titulo, conteudo):
        hoje = datetime.now().strftime("%d-%m-%Y_%H%M")
        arquivo = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("Arquivo Texto", "*.txt")],
            initialfile=f"{titulo}_{hoje}")
        if arquivo:
            with open(arquivo, 'w', encoding='utf-8') as f: f.write(conteudo)
            messagebox.showinfo("Salvo", "Arquivo gerado!")

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
        try:
            self.cursor.execute(f"SELECT nome FROM {tabela} WHERE nome LIKE ? LIMIT 6", (f'%{texto}%',))
            sugestoes = [r[0] for r in self.cursor.fetchall()]
        except sqlite3.Error: return 
        if not sugestoes: self.fechar_sugestoes(); return
        
        if not self.lista_sugestoes:
            self.lista_sugestoes = ctk.CTkToplevel(self)
            self.lista_sugestoes.overrideredirect(True)
            self.lista_sugestoes.transient(self); self.lista_sugestoes.lift()
        
        x = campo.winfo_rootx(); y = campo.winfo_rooty() + campo.winfo_height()
        self.lista_sugestoes.geometry(f"{campo.winfo_width()}x{len(sugestoes)*35}+{x}+{y}")
        for w in self.lista_sugestoes.winfo_children(): w.destroy()
        for nome in sugestoes:
            ctk.CTkButton(self.lista_sugestoes, text=nome, anchor="w", fg_color="#333", hover_color="#555",
                         command=lambda n=nome: self.selecionar_sugestao(campo, n, tabela)).pack(fill="x")

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
        self.txt_venda = ctk.CTkTextbox(self.f_dir, font=("Consolas", 14)); self.txt_venda.pack(fill="both", expand=True, padx=5, pady=5)
        
        f_tot = ctk.CTkFrame(self.f_dir); f_tot.pack(fill="x", pady=5)
        self.lbl_troco = ctk.CTkLabel(f_tot, text="TROCO: --", font=("Arial", 14, "bold"), text_color="green"); self.lbl_troco.pack(side="left", padx=10)
        self.lbl_total = ctk.CTkLabel(f_tot, text="TOTAL: R$ 0.00", font=("Arial", 24, "bold"), text_color="yellow"); self.lbl_total.pack(side="right", padx=10)
        
        self.btn_finalizar = ctk.CTkButton(self.f_dir, text="FINALIZAR E IMPRIMIR (F5)", height=50, fg_color="blue", command=self.finalizar_venda)
        self.btn_finalizar.pack(fill="x", padx=10, pady=5)

        self.ent_taxa_entrega.bind("<KeyRelease>", lambda e: self.atualizar_totais())
        self.ent_valor_pago.bind("<KeyRelease>", lambda e: self.atualizar_totais())
        self.bind("<F5>", lambda e: self.finalizar_venda())

    def montar_aba_clientes(self):
        f = ctk.CTkFrame(self.tab_clientes); f.pack(fill="x", padx=10, pady=10)
        self.ent_c_nome = ctk.CTkEntry(f, placeholder_text="Nome"); self.ent_c_nome.pack(side="left", expand=True, fill="x", padx=5)
        self.ent_c_end = ctk.CTkEntry(f, placeholder_text="Endereço"); self.ent_c_end.pack(side="left", expand=True, fill="x", padx=5)
        ctk.CTkButton(f, text="SALVAR", fg_color="green", width=100, command=self.salvar_cliente).pack(side="left", padx=5)
        self.btn_cancelar_cli = ctk.CTkButton(f, text="X", width=30, fg_color="red", command=self.cancelar_edicao_cli); self.btn_cancelar_cli.pack(side="left", padx=2); self.btn_cancelar_cli.pack_forget()

        f_acoes = ctk.CTkFrame(self.tab_clientes, fg_color="transparent"); f_acoes.pack(fill="x", padx=10)
        ctk.CTkButton(f_acoes, text="EDITAR", fg_color="#007bff", command=self.carregar_cliente_para_edicao).pack(side="left", padx=5)
        ctk.CTkButton(f_acoes, text="EXCLUIR", fg_color="red", command=self.excluir_clientes_selecionados).pack(side="left", padx=5)
        
        self.ent_busca_cli = ctk.CTkEntry(self.tab_clientes, placeholder_text="Buscar Cliente..."); self.ent_busca_cli.pack(fill="x", padx=10, pady=5)
        self.ent_busca_cli.bind("<KeyRelease>", lambda e: self.atualizar_lista_clientes())
        self.scroll_clientes = ctk.CTkScrollableFrame(self.tab_clientes); self.scroll_clientes.pack(fill="both", expand=True, padx=10, pady=5)
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
        ctk.CTkLabel(f_entrada, text="🚚 CHEGADA DE MERCADORIA (Adicionar ao Estoque)", font=("Arial", 12, "bold"), text_color="#FF9800").pack(anchor="w", padx=10, pady=5)
        
        f_ent_cont = ctk.CTkFrame(f_entrada, fg_color="transparent"); f_ent_cont.pack(fill="x", padx=5, pady=5)
        self.ent_entrada_busca = ctk.CTkEntry(f_ent_cont, placeholder_text="Digite o nome para adicionar estoque..."); self.ent_entrada_busca.pack(side="left", fill="x", expand=True, padx=5)
        self.ent_entrada_busca.bind("<KeyRelease>", lambda e: self.mostrar_autocomplete(e, self.ent_entrada_busca, "prods"))
        
        self.ent_entrada_qtd = ctk.CTkEntry(f_ent_cont, placeholder_text="Qtd Chegou", width=100); self.ent_entrada_qtd.pack(side="left", padx=5)
        ctk.CTkButton(f_ent_cont, text="➕ REGISTRAR ENTRADA", fg_color="#FF9800", text_color="black", command=self.dar_entrada_estoque).pack(side="left", padx=5)

        f_busca = ctk.CTkFrame(self.tab_produtos); f_busca.pack(fill="x", padx=10, pady=5)
        self.ent_busca_prod = ctk.CTkEntry(f_busca, placeholder_text="🔍 Filtrar tabela..."); self.ent_busca_prod.pack(side="left", fill="x", expand=True, padx=5)
        self.ent_busca_prod.bind("<KeyRelease>", lambda e: self.atualizar_lista_produtos())
        
        ctk.CTkButton(f_busca, text="✏ EDITAR SELECIONADO", width=150, fg_color="#007bff", command=self.carregar_produto_para_edicao).pack(side="left", padx=5)
        ctk.CTkButton(f_busca, text="🗑 EXCLUIR", width=100, fg_color="red", command=self.excluir_produtos_selecionados).pack(side="left", padx=5)
        ctk.CTkButton(f_busca, text="📄 RELATÓRIO PDF/TXT", width=150, fg_color="#555", command=self.relatorio_estoque_completo).pack(side="right", padx=5)
        
        f_head = ctk.CTkFrame(self.tab_produtos, height=30, fg_color="#333"); f_head.pack(fill="x", padx=10, pady=(5,0))
        ctk.CTkLabel(f_head, text="PRODUTO", width=300, anchor="w").pack(side="left", padx=35)
        ctk.CTkLabel(f_head, text="PREÇO", width=100).pack(side="left")
        ctk.CTkLabel(f_head, text="ESTOQUE ATUAL", width=120, font=("Arial", 11, "bold")).pack(side="left")
        ctk.CTkLabel(f_head, text="SAÍDA (HOJE)", width=120, text_color="#FF9800").pack(side="left")
        
        self.scroll_produtos = ctk.CTkScrollableFrame(self.tab_produtos); self.scroll_produtos.pack(fill="both", expand=True, padx=10, pady=2)
        self.atualizar_lista_produtos()

    def montar_aba_relatorio(self):
        f = ctk.CTkFrame(self.tab_relatorio); f.pack(fill="x", padx=10, pady=10)
        self.ent_data_rel = ctk.CTkEntry(f, placeholder_text="Data (DD/MM/YYYY)"); self.ent_data_rel.pack(side="left", padx=5)
        self.ent_data_rel.bind("<KeyRelease>", lambda e: self.atualizar_lista_vendas())
        
        ctk.CTkButton(f, text="IMPRIMIR FECHAMENTO", fg_color="orange", command=self.relatorio_fechamento_dia).pack(side="right", padx=5)
        ctk.CTkButton(f, text="💾 SALVAR RELATÓRIO DIA", fg_color="#555", command=self.salvar_relatorio_dia).pack(side="right", padx=5)
        
        f_acoes = ctk.CTkFrame(self.tab_relatorio, fg_color="transparent"); f_acoes.pack(fill="x", padx=10)
        ctk.CTkButton(f_acoes, text="REIMPRIMIR CUPOM", fg_color="grey", command=self.reimprimir_cupom).pack(side="left", padx=5)
        ctk.CTkButton(f_acoes, text="EDITAR VENDA", fg_color="purple", command=self.editar_venda_antiga).pack(side="left", padx=5)
        ctk.CTkButton(f_acoes, text="EXCLUIR", fg_color="red", command=self.excluir_vendas_selecionadas).pack(side="left", padx=5)
        
        self.scroll_rel = ctk.CTkScrollableFrame(self.tab_relatorio, label_text="Histórico"); self.scroll_rel.pack(fill="both", expand=True, padx=10, pady=10)
        self.atualizar_lista_vendas()

    # --- LÓGICA DE NEGÓCIO ---

    def add_item(self):
        nome = self.ent_prod_venda.get().upper()
        if not nome: return
        self.cursor.execute("SELECT preco, estoque FROM prods WHERE nome=?", (nome,))
        res = self.cursor.fetchone()
        if res:
            q_solicitada = self.safe_float(self.ent_qtd.get()) or 1
            estoque_atual = res[1]
            if estoque_atual < q_solicitada:
                if not messagebox.askyesno("Estoque Baixo", f"ATENÇÃO: Só há {estoque_atual} un.\nVender mesmo assim?"): return

            sub = q_solicitada * res[0]
            self.carrinho.append({"n": nome, "q": q_solicitada, "t": sub})
            self.total_itens += sub
            self.txt_venda.insert("end", f"{q_solicitada}x {nome} R${sub:.2f}\n")
            self.atualizar_totais()
            self.ent_prod_venda.delete(0, 'end'); self.ent_qtd.delete(0, 'end'); self.ent_qtd.insert(0, "1"); self.ent_prod_venda.focus()
        else:
            messagebox.showwarning("Erro", "Produto não encontrado")

    def atualizar_totais(self):
        tx = self.safe_float(self.ent_taxa_entrega.get())
        pg = self.safe_float(self.ent_valor_pago.get())
        tot = self.total_itens + tx
        if self.forma_pagamento.get() == "Dinheiro" and pg > 0:
            self.lbl_troco.configure(text=f"TROCO: R$ {pg - tot:.2f}")
        else:
            self.lbl_troco.configure(text="TROCO: --")
        self.lbl_total.configure(text=f"TOTAL: R$ {tot:.2f}")

    def finalizar_venda(self):
        if not self.carrinho: return
        cli = self.ent_cli.get().upper() or "CONSUMIDOR"
        end = self.ent_end.get().upper()
        pgto = self.forma_pagamento.get()
        tx = self.safe_float(self.ent_taxa_entrega.get())
        total = self.total_itens + tx
        val_pago = self.safe_float(self.ent_valor_pago.get())
        data_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
        
        if cli != "CONSUMIDOR":
            self.cursor.execute("SELECT id FROM clientes WHERE nome=?", (cli,))
            if not self.cursor.fetchone():
                self.cursor.execute("INSERT INTO clientes (nome, endereco) VALUES (?,?)", (cli, end))

        try:
            for item in self.carrinho:
                self.cursor.execute("UPDATE prods SET estoque = estoque - ? WHERE nome = ?", (item['q'], item['n']))

            if self.id_venda_em_edicao:
                self.cursor.execute("UPDATE historico SET data=?, cliente=?, total=?, forma=?, taxa=?, itens_json=? WHERE id=?",
                                   (data_hora, cli, total, pgto, tx, json.dumps(self.carrinho), self.id_venda_em_edicao))
                self.id_venda_em_edicao = None
                self.lbl_modo_edicao.pack_forget()
                self.btn_finalizar.configure(text="FINALIZAR E IMPRIMIR (F5)", fg_color="blue")
            else:
                self.cursor.execute("INSERT INTO historico (data, cliente, total, forma, taxa, itens_json) VALUES (?,?,?,?,?,?)",
                                   (data_hora, cli, total, pgto, tx, json.dumps(self.carrinho)))
            
            self.conn.commit()
            
            cupom = self.gerar_layout_cupom(self.cursor.lastrowid if not self.id_venda_em_edicao else 0, cli, end, self.carrinho, self.total_itens, tx, total, pgto, val_pago)
            self.enviar_para_impressora(cupom)
            
            self.carrinho = []; self.total_itens = 0.0; self.txt_venda.delete("1.0", "end")
            self.ent_cli.delete(0, 'end'); self.ent_end.delete(0, 'end')
            self.ent_taxa_entrega.delete(0, 'end'); self.ent_valor_pago.delete(0, 'end')
            self.atualizar_totais(); self.atualizar_lista_vendas(); self.atualizar_lista_produtos()
            messagebox.showinfo("Sucesso", "Venda Salva!")
            
        except sqlite3.Error as e:
            messagebox.showerror("Erro", str(e))

    # --- GERENCIAMENTO DE PRODUTOS E ESTOQUE ---
    def salvar_produto(self):
        nome = self.ent_p_nome.get().upper()
        preco = self.safe_float(self.ent_p_preco.get())
        if not nome: return
        
        if self.id_prod_em_edicao:
            # Edição (Não altera estoque aqui)
            self.cursor.execute("UPDATE prods SET nome=?, preco=? WHERE id=?", (nome, preco, self.id_prod_em_edicao))
            self.cancelar_edicao_prod()
        else:
            # Novo Produto
            self.cursor.execute("INSERT INTO prods (nome, preco, estoque) VALUES (?,?,0)", (nome, preco))
            
        self.conn.commit(); self.atualizar_lista_produtos()
        self.ent_p_nome.delete(0,'end'); self.ent_p_preco.delete(0,'end')

    def dar_entrada_estoque(self):
        nome = self.ent_entrada_busca.get().upper()
        qtd = self.safe_int(self.ent_entrada_qtd.get())
        data_hora = datetime.now().strftime("%d/%m/%Y %H:%M")
        
        if not nome or qtd <= 0:
            return messagebox.showwarning("Erro", "Selecione o produto e digite uma quantidade válida.")
            
        self.cursor.execute("SELECT id, estoque FROM prods WHERE nome=?", (nome,))
        res = self.cursor.fetchone()
        
        if res:
            novo_estoque = res[1] + qtd
            self.cursor.execute("UPDATE prods SET estoque=? WHERE id=?", (novo_estoque, res[0]))
            # REGISTRA A ENTRADA NO HISTORICO DE ENTRADAS
            self.cursor.execute("INSERT INTO entradas_estoque (nome_prod, qtd, data) VALUES (?,?,?)", (nome, qtd, data_hora))
            self.conn.commit()
            messagebox.showinfo("Sucesso", f"Adicionado {qtd}un ao estoque de {nome}.\nTotal agora: {novo_estoque}")
            self.ent_entrada_busca.delete(0, 'end'); self.ent_entrada_qtd.delete(0, 'end')
            self.atualizar_lista_produtos()
        else:
            messagebox.showerror("Erro", "Produto não cadastrado.")

    def calcular_movimentacoes_hoje(self):
        # Retorna { "AGUA": {'saiu': 10, 'entrou': 20}, ... }
        hoje = datetime.now().strftime("%d/%m/%Y")
        
        # 1. Calcular Saídas (Vendas)
        self.cursor.execute("SELECT itens_json FROM historico WHERE data LIKE ?", (f"{hoje}%",))
        vendas = self.cursor.fetchall()
        dados = {} # chave: nome, valor: {'saiu':0, 'entrou':0}
        
        for (json_str,) in vendas:
            try:
                itens = json.loads(json_str)
                for item in itens:
                    nome = item['n']
                    qtd = item['q']
                    if nome not in dados: dados[nome] = {'saiu':0, 'entrou':0}
                    dados[nome]['saiu'] += qtd
            except: pass
            
        # 2. Calcular Entradas (Chegada Mercadoria)
        self.cursor.execute("SELECT nome_prod, qtd FROM entradas_estoque WHERE data LIKE ?", (f"{hoje}%",))
        entradas = self.cursor.fetchall()
        for nome, qtd in entradas:
            if nome not in dados: dados[nome] = {'saiu':0, 'entrou':0}
            dados[nome]['entrou'] += qtd
            
        return dados

    def atualizar_lista_produtos(self):
        for w in self.scroll_produtos.winfo_children(): w.destroy()
        self.check_vars_prod = {}
        b = self.ent_busca_prod.get().upper()
        
        movs = self.calcular_movimentacoes_hoje()
        
        self.cursor.execute("SELECT id, nome, preco, estoque FROM prods WHERE nome LIKE ? ORDER BY nome", (f'%{b}%',))
        for pid, nome, preco, estoque in self.cursor.fetchall():
            row = ctk.CTkFrame(self.scroll_produtos, fg_color="transparent")
            row.pack(fill="x", pady=2)
            
            var = ctk.StringVar(value="off")
            self.check_vars_prod[pid] = var
            ctk.CTkCheckBox(row, text="", variable=var, onvalue="on", offvalue="off", width=20).pack(side="left")
            
            cor_estoque = "#28a745" if estoque > 10 else "#f44336"
            qtd_saiu = movs.get(nome, {}).get('saiu', 0)
            
            ctk.CTkLabel(row, text=f"{nome}", width=300, anchor="w", font=("Arial", 12, "bold")).pack(side="left", padx=5)
            ctk.CTkLabel(row, text=f"R$ {preco:.2f}", width=100).pack(side="left")
            ctk.CTkLabel(row, text=f"{estoque} un", width=120, text_color=cor_estoque, font=("Arial", 12, "bold")).pack(side="left")
            ctk.CTkLabel(row, text=f"- {int(qtd_saiu)} saídas", width=120, text_color="#FF9800").pack(side="left")

    def excluir_produtos_selecionados(self):
        ids = [k for k, v in self.check_vars_prod.items() if v.get() == "on"]
        if not ids: return
        if messagebox.askyesno("Confirmar", "Excluir produtos selecionados?"):
            self.cursor.execute(f"DELETE FROM prods WHERE id IN ({','.join('?'*len(ids))})", ids)
            self.conn.commit(); self.atualizar_lista_produtos()

    def carregar_produto_para_edicao(self):
        ids = [k for k, v in self.check_vars_prod.items() if v.get() == "on"]
        if len(ids) != 1: return messagebox.showwarning("Erro", "Selecione apenas 1 produto")
        self.cursor.execute("SELECT nome, preco FROM prods WHERE id=?", (ids[0],))
        r = self.cursor.fetchone()
        self.ent_p_nome.delete(0,'end'); self.ent_p_nome.insert(0, r[0])
        self.ent_p_preco.delete(0,'end'); self.ent_p_preco.insert(0, r[1])
        self.id_prod_em_edicao = ids[0]
        self.btn_salvar_prod.configure(text="SALVAR ALTERAÇÕES", fg_color="blue")
        self.btn_cancelar_prod.pack(side="left", padx=2)

    def cancelar_edicao_prod(self):
        self.id_prod_em_edicao = None
        self.ent_p_nome.delete(0,'end'); self.ent_p_preco.delete(0,'end')
        self.btn_salvar_prod.configure(text="SALVAR / CRIAR", fg_color="green")
        self.btn_cancelar_prod.pack_forget()

    def relatorio_estoque_completo(self):
        # Relatório Completo com Entradas e Saídas
        self.cursor.execute("SELECT nome, estoque FROM prods ORDER BY nome")
        produtos = self.cursor.fetchall()
        movs = self.calcular_movimentacoes_hoje()
        
        txt = "RELATORIO DETALHADO DE ESTOQUE\n"
        txt += f"Data: {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        txt += "="*75 + "\n"
        txt += f"{'PRODUTO':<30} {'ATUAL':^10} {'ENTROU(DIA)':^15} {'SAIU(DIA)':^15}\n"
        txt += "-"*75 + "\n"
        
        for nome, estoque_atual in produtos:
            entrou = movs.get(nome, {}).get('entrou', 0)
            saiu = movs.get(nome, {}).get('saiu', 0)
            
            txt += f"{nome[:30]:<30} {estoque_atual:^10} {entrou:^15} {saiu:^15}\n"
            
        txt += "="*75 + "\n"
        
        opcao = messagebox.askyesno("Opções", "Sim = Imprimir na Térmica\nNão = Salvar em Arquivo")
        if opcao: self.enviar_para_impressora(txt)
        else: self.salvar_arquivo_generico("Relatorio_Estoque_Detalhado", txt)

    # --- RELATÓRIOS E CLIENTES (Mantidos) ---
    def salvar_cliente(self):
        nome = self.ent_c_nome.get().upper(); end = self.ent_c_end.get().upper()
        if not nome: return
        if self.id_cliente_em_edicao:
            self.cursor.execute("UPDATE clientes SET nome=?, endereco=? WHERE id=?", (nome, end, self.id_cliente_em_edicao))
            self.cancelar_edicao_cli()
        else: self.cursor.execute("INSERT INTO clientes (nome, endereco) VALUES (?,?)", (nome, end))
        self.conn.commit(); self.atualizar_lista_clientes(); self.ent_c_nome.delete(0,'end'); self.ent_c_end.delete(0,'end')

    def buscar_endereco_cliente(self):
        self.cursor.execute("SELECT endereco FROM clientes WHERE nome=?", (self.ent_cli.get().upper(),))
        r = self.cursor.fetchone()
        if r: self.ent_end.delete(0, 'end'); self.ent_end.insert(0, r[0])

    def atualizar_lista_clientes(self):
        for w in self.scroll_clientes.winfo_children(): w.destroy()
        self.check_vars_cli = {}
        b = self.ent_busca_cli.get().upper()
        self.cursor.execute("SELECT id, nome, endereco FROM clientes WHERE nome LIKE ?", (f'%{b}%',))
        for cid, nome, end in self.cursor.fetchall():
            row = ctk.CTkFrame(self.scroll_clientes, fg_color="transparent"); row.pack(fill="x", pady=2)
            var = ctk.StringVar(value="off"); self.check_vars_cli[cid] = var
            ctk.CTkCheckBox(row, text="", variable=var, onvalue="on", offvalue="off", width=20).pack(side="left")
            ctk.CTkLabel(row, text=f"{nome} | {end}", anchor="w").pack(side="left", fill="x")

    def excluir_clientes_selecionados(self):
        ids = [k for k, v in self.check_vars_cli.items() if v.get() == "on"]
        if not ids: return
        if messagebox.askyesno("Confirmar", "Excluir?"):
            self.cursor.execute(f"DELETE FROM clientes WHERE id IN ({','.join('?'*len(ids))})", ids)
            self.conn.commit(); self.atualizar_lista_clientes()

    def carregar_cliente_para_edicao(self):
        ids = [k for k, v in self.check_vars_cli.items() if v.get() == "on"]
        if len(ids) != 1: return
        self.cursor.execute("SELECT nome, endereco FROM clientes WHERE id=?", (ids[0],))
        r = self.cursor.fetchone()
        self.ent_c_nome.delete(0,'end'); self.ent_c_nome.insert(0, r[0])
        self.ent_c_end.delete(0,'end'); self.ent_c_end.insert(0, r[1])
        self.id_cliente_em_edicao = ids[0]
        self.btn_salvar_cli.configure(text="SALVAR ALTERAÇÕES", fg_color="blue")
        self.btn_cancelar_cli.pack(side="left", padx=2)

    def cancelar_edicao_cli(self):
        self.id_cliente_em_edicao = None; self.ent_c_nome.delete(0,'end'); self.ent_c_end.delete(0,'end')
        self.btn_salvar_cli.configure(text="SALVAR", fg_color="green"); self.btn_cancelar_cli.pack_forget()

    def atualizar_lista_vendas(self):
        for w in self.scroll_rel.winfo_children(): w.destroy()
        self.check_vars_rel = {}
        data = self.ent_data_rel.get()
        query = "SELECT id, data, cliente, total FROM historico WHERE 1=1"
        params = []
        if data: query += " AND data LIKE ?"; params.append(f"%{data}%")
        query += " ORDER BY id DESC LIMIT 50"
        self.cursor.execute(query, params)
        for vid, d, c, t in self.cursor.fetchall():
            row = ctk.CTkFrame(self.scroll_rel, fg_color="transparent"); row.pack(fill="x", pady=2)
            var = ctk.StringVar(value="off"); self.check_vars_rel[vid] = var
            ctk.CTkCheckBox(row, text="", variable=var, onvalue="on", offvalue="off", width=20).pack(side="left")
            ctk.CTkLabel(row, text=f"#{vid} {d} | {c} | R$ {t:.2f}", anchor="w").pack(side="left", fill="x")

    def excluir_vendas_selecionadas(self):
        ids = [k for k, v in self.check_vars_rel.items() if v.get() == "on"]
        if not ids: return
        if messagebox.askyesno("CUIDADO", "Apagar?"):
            self.cursor.execute(f"DELETE FROM historico WHERE id IN ({','.join('?'*len(ids))})", ids)
            self.conn.commit(); self.atualizar_lista_vendas()

    def reimprimir_cupom(self):
        ids = [k for k, v in self.check_vars_rel.items() if v.get() == "on"]
        if len(ids) != 1: return
        self.cursor.execute("SELECT * FROM historico WHERE id=?", (ids[0],))
        r = self.cursor.fetchone() 
        carrinho = json.loads(r[6])
        cupom = self.gerar_layout_cupom(r[0], r[2], "", carrinho, r[3]-r[5], r[5], r[3], r[4])
        self.enviar_para_impressora(cupom)

    def editar_venda_antiga(self):
        ids = [k for k, v in self.check_vars_rel.items() if v.get() == "on"]
        if len(ids) != 1: return
        if not messagebox.askyesno("Confirmar", "Editar venda?"): return
        self.cursor.execute("SELECT * FROM historico WHERE id=?", (ids[0],))
        r = self.cursor.fetchone()
        self.id_venda_em_edicao = r[0]
        self.ent_cli.delete(0,'end'); self.ent_cli.insert(0, r[2])
        self.forma_pagamento.set(r[4])
        self.ent_taxa_entrega.delete(0,'end'); self.ent_taxa_entrega.insert(0, str(r[5]))
        self.carrinho = json.loads(r[6])
        self.txt_venda.delete("1.0", "end")
        self.total_itens = 0
        for item in self.carrinho:
            self.total_itens += item['t']
            self.txt_venda.insert("end", f"{item['q']}x {item['n']} R${item['t']:.2f}\n")
        self.atualizar_totais(); self.lbl_modo_edicao.pack(fill="x", pady=5)
        self.btn_finalizar.configure(text=f"SALVAR ALTERAÇÃO #{r[0]}", fg_color="red")
        self.abas.set("Venda")

    def salvar_relatorio_dia(self):
        data = self.ent_data_rel.get().strip() or datetime.now().strftime("%d/%m/%Y")
        self.cursor.execute("SELECT forma, SUM(total) FROM historico WHERE data LIKE ? GROUP BY forma", (f"{data}%",))
        dados = self.cursor.fetchall()
        if not dados: return messagebox.showwarning("Aviso", "Sem vendas nesta data")
        
        txt = f"RELATORIO FECHAMENTO - {data}\n" + "="*40 + "\n"
        geral = 0.0
        for f, v in dados:
            txt += f"{f:<20} R$ {v:>10.2f}\n"; geral += v
        txt += "="*40 + "\n" + f"TOTAL: R$ {geral:.2f}"
        self.salvar_arquivo_generico(f"Fechamento_{data.replace('/','-')}", txt)

    def relatorio_fechamento_dia(self):
        hoje = datetime.now().strftime("%d/%m/%Y")
        self.cursor.execute("SELECT forma, SUM(total) FROM historico WHERE data LIKE ? GROUP BY forma", (f"{hoje}%",))
        dados = self.cursor.fetchall()
        txt = "\nFECHAMENTO DO DIA\n" + f"DATA: {hoje}\n" + "="*48 + "\n"
        geral = 0.0
        for f, v in dados: txt += f"{f:<30} R$ {v:>13.2f}\n"; geral += v
        txt += "="*48 + "\n" + f"TOTAL: R$ {geral:.2f}\n\n\n"
        self.enviar_para_impressora(txt)

if __name__ == "__main__":
    app = SistemaComanda()
    app.mainloop()