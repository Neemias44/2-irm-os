# Sistema Dois Irmãos

Sistema de vendas e estoque para a distribuidora de água e gás Dois Irmãos.

## Funcionalidades

- **Venda:** carrinho com autocomplete, taxa de entrega, troco, cupom na impressora térmica (F5) ou só salvar (F4).
- **Clientes:** cadastro e busca por **nome ou endereço** (a busca ignora acentos).
- **Estoque:** cadastro de produtos, estoque atual, saídas do dia e filtro "só estoque baixo".
- **Entrada de Mercadoria:** registro das chegadas, mostrando o estoque atual do produto, e histórico das entradas. Excluir uma entrada lançada errado retira a quantidade do estoque.
- **Histórico de Vendas:** busca por número, data ou cliente; reimpressão (2ª via), edição (duplo clique) e exclusão (a exclusão devolve os itens ao estoque).
- **Relatórios (painel):** escolha **Diário, Semanal (segunda a domingo), Mensal, Anual** ou **Personalizado** (datas de início e fim); ◀ ▶ andam para o período anterior ou o próximo. O painel muda na hora:
  - cartões de faturamento, vendas, ticket médio, itens, entregas e taxas, com ▲▼ comparando ao período anterior;
  - gráfico de faturamento por hora (no Diário), por dia ou por mês (em períodos longos);
  - formas de pagamento (valor e %) e produtos mais vendidos;
  - passe o mouse numa barra para ver os detalhes;
  - **GERAR PDF** e **IMPRIMIR RESUMO** usam o mesmo período.
- **Backup:** automático uma vez por dia e manual pelo botão "SALVAR INFORMAÇÕES", na pasta `backups/`.
- **Backup no Drive (fora do computador):** botão "☁️ BACKUP NO DRIVE" (o ⚙ ao lado troca a pasta). Na primeira vez, escolha a pasta do Google Drive, do OneDrive ou do pendrive. Depois disso:
  - é feito **sozinho ao fechar o programa** no fim do dia;
  - se o PC foi desligado sem fechar o programa, é feito na próxima vez que ele abrir;
  - só envia se algo mudou desde a última cópia;
  - na pasta `Backup Dois Irmaos` ficam as 3 cópias mais recentes, numeradas: `1_loja_...` é sempre a mais nova (data e hora no nome). A cada cópia nova, a 3 é apagada e as outras andam uma posição;
  - a pasta escolhida fica em `config.json`.

## Como rodar

```bash
pip install -r requirements.txt
python app.py
```

O banco `loja.db` é criado na pasta de onde o programa é executado.

## Gerar o executável

```bash
pyinstaller app.spec
```

## Banco de dados

- Todo o acesso ao banco passa por uma única thread dedicada (`banco.py`). A tela nunca espera o banco, e cada operação é gravada por inteiro ou não é gravada.
- Os itens de cada venda ficam na tabela `itens_venda`, ligados ao produto pelo ID. Por isso, renomear um produto não quebra o estoque nem os relatórios.
- As datas também são gravadas em formato ordenável (`data_iso`), com índice, e os relatórios por período são calculados direto no SQL.
- Bancos antigos são atualizados sozinhos na primeira abertura. Antes disso é feita uma cópia em `backups/loja_antes_v2_*.db`.
- O banco usa o modo WAL, então backups e relatórios podem ler enquanto o sistema grava. Enquanto o programa está aberto, existem os arquivos `loja.db-wal` e `loja.db-shm`. **Para copiar o banco para outro PC, feche o programa antes** (ou use o botão "SALVAR INFORMAÇÕES" e copie o backup).

## Arquivos

| Arquivo | Para que serve |
|---|---|
| `app.py` | Sistema principal (telas) |
| `banco.py` | Banco de dados: estrutura, migração e todas as consultas |
| `painel.py` | Painel da aba Relatórios (cartões e gráficos desenhados na tela, sem bibliotecas extras) |
| `vigia.py` | Registra em `travamentos.log` toda vez que a tela para por mais de 0,5 s (com a causa) e os erros que antes só apareciam no terminal |
| `verificar.py` | Mostra as tabelas e a quantidade de registros do `loja.db` |
| `site.py` | Versão antiga do sistema (não é mais usada) |
