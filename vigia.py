"""
Vigia de travamentos.

Fica em segundo plano e, sempre que a tela parar de responder por mais de
LIMITE segundos, grava em 'travamentos.log':
  * quanto tempo a tela ficou parada;
  * o último clique/tecla do usuário antes da parada;
  * em que linha do código o programa estava (amostrado várias vezes
    durante a parada, para mostrar onde o tempo realmente foi gasto).

Também grava erros que antes só apareciam no terminal (e que, para quem
usa, pareciam "o botão não fez nada" ou "travou").

Custo: uma thread acordando a cada 50 ms. Não deixa o programa mais lento.
"""
import collections
import os
import sys
import threading
import time
import traceback
from datetime import datetime

_PASTA_DO_PROGRAMA = os.path.dirname(os.path.abspath(__file__)).lower()
_TAMANHO_MAXIMO = 512 * 1024   # se o log passar disso, começa um novo


def _nosso(frame_summary):
    return os.path.abspath(frame_summary.filename).lower().startswith(_PASTA_DO_PROGRAMA)


class VigiaTravamentos:
    def __init__(self, root, arquivo="travamentos.log", limite=0.5):
        self.root, self.arquivo, self.limite = root, arquivo, limite
        self._batida = time.perf_counter()
        self._ultima_acao = "(nenhuma ainda)"
        self._main = threading.main_thread().ident
        self._trava_lock = threading.Lock()
        self._armado = False   # só vigia depois que a janela terminou de abrir

        root.bind_all("<ButtonPress>", self._registrar_acao, add="+")
        root.bind_all("<KeyPress>", self._registrar_acao, add="+")
        # Erros dentro de callbacks da tela e de threads
        root.report_callback_exception = self._erro_na_tela
        threading.excepthook = self._erro_em_thread

        self._tic()
        root.after(3000, self._armar)
        threading.Thread(target=self._vigiar, name="vigia", daemon=True).start()

    def _armar(self):
        self._batida = time.perf_counter()
        self._armado = True

    # --- batida do coração da tela ---
    def _tic(self):
        self._batida = time.perf_counter()
        try:
            self.root.after(100, self._tic)
        except Exception:
            pass

    # --- o que o usuário fez por último ---
    def _registrar_acao(self, e):
        try:
            w = e.widget
            alvo = ""
            for obj in (w, getattr(w, "master", None)):
                for opcao, prefixo in (("text", "botão/rótulo "), ("placeholder_text", "campo ")):
                    try:
                        texto = obj.cget(opcao)
                    except Exception:
                        continue
                    if texto:
                        alvo = f'{prefixo}"{texto}"'
                        break
                if alvo:
                    break
            if not alvo:
                alvo = type(getattr(w, "master", w)).__name__ if "ctk" in str(w) else str(w)[-60:]
            eh_tecla = getattr(e.type, "name", str(e.type)) in ("KeyPress", "2")
            if eh_tecla:
                tecla = e.keysym if len(e.keysym) > 1 else "tecla de texto"
                acao = f"tecla {tecla} em {alvo}"
            else:
                acao = f"clique em {alvo}"
            # aba atual ajuda a entender o contexto
            aba = ""
            try:
                aba = f" (aba {self.root.abas.get()})"
            except Exception:
                pass
            self._ultima_acao = f"{datetime.now():%H:%M:%S} {acao}{aba}"
        except Exception:
            pass

    # --- thread do vigia ---
    def _vigiar(self):
        amostras = None
        inicio = None
        pilha_exemplo = None
        while True:
            time.sleep(0.05)
            parado = time.perf_counter() - self._batida
            if self._armado and parado > self.limite:
                if amostras is None:
                    amostras, inicio, pilha_exemplo = collections.Counter(), self._batida, None
                    avisos_parciais = 0
                # Travou de vez? Grava JÁ (aos 3 s e depois a cada 10 s): se o usuário
                # fechar o programa à força, o motivo fica registrado mesmo assim.
                if pilha_exemplo is not None and parado > 3 + 10 * avisos_parciais:
                    self._gravar_trava(parado, amostras, pilha_exemplo, ainda_travada=True)
                    avisos_parciais += 1
                frame = sys._current_frames().get(self._main)
                if frame is not None:
                    pilha = traceback.extract_stack(frame)
                    nossas = [f for f in pilha if _nosso(f)]
                    ponto = nossas[-1] if nossas else pilha[-1]
                    fora = pilha[-1]
                    chave = f"{os.path.basename(ponto.filename)}:{ponto.lineno} em {ponto.name}()"
                    if fora is not ponto:
                        chave += f"  ->  {os.path.basename(fora.filename)}:{fora.lineno} {fora.name}()"
                    amostras[chave] += 1
                    pilha_exemplo = pilha
            elif amostras is not None:
                duracao = self._batida - inicio   # a nova batida marca a hora em que a tela voltou
                self._gravar_trava(duracao, amostras, pilha_exemplo)
                amostras = None

    def _gravar_trava(self, duracao, amostras, pilha, ainda_travada=False):
        total = sum(amostras.values()) or 1
        titulo = (f"TELA TRAVADA HÁ {duracao:.0f} s (ainda sem responder)" if ainda_travada
                  else f"TELA PARADA POR {duracao:.1f} s")
        linhas = [f"[{datetime.now():%d/%m/%Y %H:%M:%S}] {titulo}",
                  f"  Última ação do usuário: {self._ultima_acao}",
                  "  Onde o programa estava (amostras):"]
        for chave, n in amostras.most_common(4):
            linhas.append(f"    {n * 100 // total:3d}%  {chave}")
        if pilha:
            linhas.append("  Caminho completo (última amostra):")
            for f in pilha[-12:]:
                marca = "*" if _nosso(f) else " "
                linhas.append(f"   {marca} {os.path.basename(f.filename)}:{f.lineno} {f.name}()  {(f.line or '')[:90]}")
        self._escrever("\n".join(linhas))

    # --- erros silenciosos ---
    def _erro_na_tela(self, tipo, valor, tb):
        texto = "".join(traceback.format_exception(tipo, valor, tb))
        self._escrever(f"[{datetime.now():%d/%m/%Y %H:%M:%S}] ERRO NA TELA\n"
                       f"  Última ação do usuário: {self._ultima_acao}\n{texto}")
        traceback.print_exception(tipo, valor, tb)

    def _erro_em_thread(self, args):
        if args.exc_type is SystemExit:
            return
        texto = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        nome = args.thread.name if args.thread else "?"
        self._escrever(f"[{datetime.now():%d/%m/%Y %H:%M:%S}] ERRO NA THREAD '{nome}'\n{texto}")
        traceback.print_exception(args.exc_type, args.exc_value, args.exc_traceback)

    def _escrever(self, bloco):
        with self._trava_lock:
            try:
                if os.path.exists(self.arquivo) and os.path.getsize(self.arquivo) > _TAMANHO_MAXIMO:
                    os.replace(self.arquivo, self.arquivo + ".antigo")
                with open(self.arquivo, "a", encoding="utf-8") as f:
                    f.write(bloco.rstrip() + "\n\n")
            except OSError:
                pass
            print(bloco, file=sys.stderr)
