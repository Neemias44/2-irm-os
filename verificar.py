import sqlite3
import os

# Pega o arquivo 'loja' na pasta atual
caminho = "loja"

if not os.path.exists(caminho):
    # Tenta com a extensão .db se não achar sem
    caminho = "loja.db"

if not os.path.exists(caminho):
    print(f"❌ ERRO: Não encontrei o arquivo 'loja' nem 'loja.db' na pasta.")
else:
    print(f"✅ Arquivo encontrado: {caminho}")
    try:
        conn = sqlite3.connect(caminho)
        cursor = conn.cursor()
        
        # Pergunta para o banco quais tabelas existem
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tabelas = cursor.fetchall()
        
        if not tabelas:
            print("⚠️ O banco de dados está VAZIO (sem tabelas).")
            print("Dica: Você renomeou o arquivo certo? O arquivo de 20KB pode estar vazio.")
        else:
            print("\n📋 TABELAS ENCONTRADAS:")
            for t in tabelas:
                nome_tabela = t[0]
                # Conta quantos itens tem em cada tabela
                cursor.execute(f"SELECT COUNT(*) FROM {nome_tabela}")
                qtd = cursor.fetchone()[0]
                print(f"   ➡ Tabela: '{nome_tabela}' (Tem {qtd} registros)")
                
        conn.close()
    except Exception as e:
        print(f"❌ Erro ao ler o banco: {e}")