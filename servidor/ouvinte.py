# -*- coding: utf-8 -*-
"""
Ouvinte do Tarefa IA — roda no servidor (cron, a cada 1 min).
Lê as mensagens que Bruno manda no chat do bot @TarefaIABruno_bot
(texto OU áudio), interpreta com IA e cria as tarefas no repositório
privado — as mesmas que aparecem no app do PC, no painel e nos lembretes.
"""

import json
import sys
import time
from base64 import b64decode, b64encode
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

# Modo plantonista: o processo roda PARA SEMPRE grudado no Telegram
# (long polling contínuo). O cron de 1 min vira só um vigia que o
# ressuscita se ele morrer. Mensagem nova = resposta em ~1-3s, sem buracos.
POLL_SECONDS = 25

TZ = ZoneInfo("America/Sao_Paulo")
REPO = "brunohpr-hash/tarefa-ia-dados"
GH_TOKEN_FILE = Path("/root/.github-ditado-token")
TG_TOKEN_FILE = Path("/root/.telegram-tarefa-token")
TG_CHATID_FILE = Path("/root/.telegram-tarefa-chatid")
GROQ_KEY_FILE = Path("/root/.groq-tarefa-key")
STATE_PATH = Path(__file__).resolve().parent / "bot_estado.json"

STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
STT_MODEL = "whisper-large-v3"
LLM_URL = "https://api.groq.com/openai/v1/chat/completions"
LLM_MODEL = "openai/gpt-oss-120b"

DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira",
               "quinta-feira", "sexta-feira", "sábado", "domingo"]
DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
PRIO_ICON = {"alta": "🔴", "normal": "🔵", "baixa": "⚪"}

PARSE_PROMPT = (
    "Você é um assistente que transforma fala livre (ou texto digitado) em ações "
    "sobre uma lista de tarefas. O texto pode conter VÁRIAS ações misturadas.\n"
    "Responda SOMENTE com JSON válido neste formato:\n"
    '{"adicionar": [{"titulo": "...", "prazo": "YYYY-MM-DD HH:MM" | "YYYY-MM-DD" | null, '
    '"prioridade": "alta" | "normal" | "baixa"}], '
    '"concluir": ["descrição da tarefa já feita"], '
    '"cancelar": ["descrição da tarefa a remover/cancelar"], '
    '"remarcar": [{"descricao": "tarefa citada", "prazo": "YYYY-MM-DD HH:MM" | "YYYY-MM-DD"}], '
    '"listar": true | false}\n\n'
    "REGRAS:\n"
    "- 'concluir' = coisas que a pessoa JÁ FEZ ('já comprei', 'conclui', 'feita').\n"
    "- 'cancelar' = tarefa/compromisso que FOI CANCELADO, desmarcado ou que a pessoa "
    "manda remover/excluir/apagar da lista ('a reunião foi cancelada', 'tira da lista', "
    "'não precisa mais'). Cancelar NÃO é concluir.\n"
    "- 'remarcar' = mudança de data/horário de tarefa EXISTENTE ('muda pra sexta', "
    "'adia para amanhã às 10', 'remarca a reunião para dia 15'). Use a tarefa citada "
    "em 'descricao' e o novo prazo resolvido.\n"
    "- 'listar' = true quando a pessoa pergunta o que tem na lista/agenda ('o que tenho "
    "pra hoje?', 'minhas tarefas', 'lista aí').\n"
    "- 'adicionar' = somente coisas NOVAS a fazer. NÃO adicione tarefa nova quando o "
    "texto é claramente um comando sobre tarefa existente (cancelar/remarcar/concluir).\n"
    "- 'agora' (data/hora atual e dia da semana) será informado; use-o para resolver "
    "datas relativas: amanhã, sexta, semana que vem, dia 15, daqui a 3 dias...\n"
    "- Datas relativas apontam SEMPRE para o futuro.\n"
    "- Se houver horário dito, use YYYY-MM-DD HH:MM; só o dia, YYYY-MM-DD; sem prazo, "
    "null. NUNCA invente prazo que não foi dito.\n"
    "- prioridade 'alta' só com urgência explícita; 'baixa' para coisas casuais; "
    "senão 'normal'.\n"
    "- Título curto começando com verbo no infinitivo, mantendo nomes próprios.\n"
    "- NÃO responda perguntas do texto; apenas extraia ações.\n"
    "- Sem ações no texto: tudo vazio e listar=false."
)


def log(msg):
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def tg(method, timeout_http=30, **data):
    token = TG_TOKEN_FILE.read_text().strip()
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                      data=data, timeout=timeout_http)
    r.raise_for_status()
    return r.json().get("result")


def tg_download(file_id):
    token = TG_TOKEN_FILE.read_text().strip()
    info = tg("getFile", file_id=file_id)
    path = info["file_path"]
    r = requests.get(f"https://api.telegram.org/file/bot{token}/{path}", timeout=60)
    r.raise_for_status()
    return r.content, Path(path).name


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"offset": 0}


def save_state(st):
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def agora_str():
    n = datetime.now(TZ)
    return f"{n:%Y-%m-%d %H:%M} ({DIAS_SEMANA[n.weekday()]})"


def parse_prazo(prazo):
    if not prazo:
        return None, False
    s = str(prazo).strip()
    for fmt, com_hora in (("%Y-%m-%d %H:%M", True), ("%Y-%m-%d", False)):
        try:
            return datetime.strptime(s, fmt), com_hora
        except Exception:
            continue
    return None, False


def prazo_humano(prazo):
    dt, com_hora = parse_prazo(prazo)
    if not dt:
        return "sem prazo"
    hoje = datetime.now(TZ).date()
    d = dt.date()
    if d == hoje:
        dia = "hoje"
    elif (d - hoje).days == 1:
        dia = "amanhã"
    else:
        dia = f"{dt:%d/%m}" + (f" ({DIAS[dt.weekday()]})" if 0 < (d - hoje).days < 7 else "")
    return f"{dia} às {dt:%H:%M}" if com_hora else dia


# ---------------------------------------------------------------- IA

def groq_key():
    return GROQ_KEY_FILE.read_text().strip()


def transcrever(audio_bytes, filename):
    r = requests.post(
        STT_URL,
        headers={"Authorization": f"Bearer {groq_key()}"},
        files={"file": (filename, audio_bytes)},
        data={"model": STT_MODEL, "language": "pt",
              "response_format": "text", "temperature": 0},
        timeout=120)
    r.raise_for_status()
    return r.text.strip()


def interpretar(texto, titulos_abertos=None, contexto_reply=None):
    user = f"agora: {agora_str()}\n"
    if titulos_abertos:
        lista = "\n".join(f"- {t}" for t in titulos_abertos[:30])
        user += (f"TAREFAS ABERTAS ATUAIS (para concluir/cancelar/remarcar, use o "
                 f"título EXATO desta lista):\n{lista}\n")
    if contexto_reply:
        user += (f"CONTEXTO (a pessoa está respondendo a esta mensagem do bot — "
                 f"use-o para saber de qual tarefa ela fala):\n"
                 f"<contexto>\n{contexto_reply[:600]}\n</contexto>\n")
    user += f"<texto>\n{texto}\n</texto>"
    r = requests.post(
        LLM_URL,
        headers={"Authorization": f"Bearer {groq_key()}",
                 "Content-Type": "application/json"},
        json={"model": LLM_MODEL, "temperature": 0.2,
              "response_format": {"type": "json_object"},
              "messages": [
                  {"role": "system", "content": PARSE_PROMPT},
                  {"role": "user", "content": user}]},
        timeout=90)
    r.raise_for_status()
    out = r.json()["choices"][0]["message"]["content"]
    try:
        data = json.loads(out)
    except Exception:
        i, f = out.find("{"), out.rfind("}")
        data = json.loads(out[i:f + 1]) if i >= 0 and f > i else {}
    tarefas = []
    for t in data.get("adicionar", data.get("tarefas", [])):
        titulo = str(t.get("titulo", "")).strip()
        if not titulo:
            continue
        prazo = t.get("prazo") or ""
        if prazo and parse_prazo(prazo)[0] is None:
            prazo = ""
        prio = t.get("prioridade", "normal")
        if prio not in ("alta", "normal", "baixa"):
            prio = "normal"
        tarefas.append({"titulo": titulo, "prazo": prazo, "prioridade": prio})
    concluir = [str(c).strip() for c in data.get("concluir", []) if str(c).strip()]
    cancelar = [str(c).strip() for c in data.get("cancelar", []) if str(c).strip()]
    remarcar = []
    for r_ in data.get("remarcar", []):
        desc = str(r_.get("descricao", "")).strip()
        prazo = r_.get("prazo") or ""
        if desc and prazo and parse_prazo(prazo)[0] is not None:
            remarcar.append({"descricao": desc, "prazo": prazo})
    return {"adicionar": tarefas, "concluir": concluir, "cancelar": cancelar,
            "remarcar": remarcar, "listar": bool(data.get("listar"))}


def _normaliza(s):
    import unicodedata
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def achar_tarefa(descricao, abertas):
    """Casa a descrição falada com uma tarefa aberta (palavras em comum + difflib)."""
    from difflib import SequenceMatcher
    desc = _normaliza(descricao)
    desc_palavras = {p for p in desc.split() if len(p) > 2}
    melhor, melhor_nota = None, 0.0
    for t in abertas:
        tit = _normaliza(t["titulo"])
        tit_palavras = {p for p in tit.split() if len(p) > 2}
        comuns = desc_palavras & tit_palavras
        nota_palavras = len(comuns) / max(1, min(len(desc_palavras), len(tit_palavras)))
        nota_seq = SequenceMatcher(None, desc, tit).ratio()
        nota = max(nota_palavras, nota_seq)
        if nota > melhor_nota:
            melhor, melhor_nota = t, nota
    return melhor if melhor_nota >= 0.5 else None


# ---------------------------------------------------------------- repo de dados

def gh_headers():
    return {"Authorization": f"token {GH_TOKEN_FILE.read_text().strip()}",
            "Accept": "application/vnd.github+json"}


def repo_url():
    return f"https://api.github.com/repos/{REPO}/contents/tarefas.json"


def merge_tasks(locais, remotas):
    by_id = {}
    for t in list(remotas) + list(locais):
        cur = by_id.get(t["id"])
        if cur is None or str(t.get("atualizado_em", "")) >= str(cur.get("atualizado_em", "")):
            by_id[t["id"]] = t
    return sorted(by_id.values(), key=lambda t: t.get("criada_em", ""))


def add_tasks_remote(novas):
    r = requests.get(repo_url(), headers=gh_headers(), timeout=20)
    sha = None
    remotas = []
    if r.status_code == 200:
        d = r.json()
        sha = d.get("sha")
        remotas = json.loads(b64decode(d["content"]).decode("utf-8")).get("tarefas", [])
    final = merge_tasks(novas, remotas)
    body = json.dumps({"tarefas": final,
                       "atualizado_em": datetime.now(TZ).isoformat(timespec="seconds")},
                      ensure_ascii=False, indent=2)
    payload = {"message": f"bot: +{len(novas)} tarefa(s)",
               "content": b64encode(body.encode("utf-8")).decode("ascii")}
    if sha:
        payload["sha"] = sha
    r = requests.put(repo_url(), headers=gh_headers(), json=payload, timeout=20)
    r.raise_for_status()


def nova_tarefa(titulo, prazo, prioridade):
    now = datetime.now(TZ).isoformat(timespec="seconds")
    return {"id": uuid4().hex[:12], "titulo": titulo.strip(),
            "prazo": prazo or "", "prioridade": prioridade,
            "status": "aberta", "criada_em": now, "atualizado_em": now,
            "notificada_local": True}  # avisada pelo canal; PC não precisa repetir


# ---------------------------------------------------------------- fluxo

def fetch_remote():
    r = requests.get(repo_url(), headers=gh_headers(), timeout=20)
    if r.status_code != 200:
        return [], None
    d = r.json()
    tarefas = json.loads(b64decode(d["content"]).decode("utf-8")).get("tarefas", [])
    return tarefas, d.get("sha")


def push_remote(tarefas, sha, msg):
    body = json.dumps({"tarefas": tarefas,
                       "atualizado_em": datetime.now(TZ).isoformat(timespec="seconds")},
                      ensure_ascii=False, indent=2)
    payload = {"message": msg,
               "content": b64encode(body.encode("utf-8")).decode("ascii")}
    if sha:
        payload["sha"] = sha
    r = requests.put(repo_url(), headers=gh_headers(), json=payload, timeout=20)
    r.raise_for_status()


def processar_texto(texto, chat_id, contexto_reply=None):
    # atalho direto: pedido de atualizar o painel não precisa de IA
    import re as _re
    if _re.search(r"atualiz\w*.{0,25}painel|painel.{0,25}atualiz\w*",
                  texto.lower()):
        try:
            import importlib.util as _il
            _spec = _il.spec_from_file_location(
                "vigia", str(Path(__file__).parent / "vigia.py"))
            _v = _il.module_from_spec(_spec); _spec.loader.exec_module(_v)
            _tarefas = _v.fetch_tasks()
            _st = _v.load_state()
            _st.pop("painel_hash", None)   # força redesenho completo
            from datetime import datetime as _dt
            _v.atualizar_painel(_tarefas, _dt.now(_v.TZ), _st)
            _v.save_state(_st)
            abertas = sum(1 for t in _tarefas if t.get("status") == "aberta")
            tg("sendMessage", chat_id=chat_id,
               text=f"🔄 Painel redesenhado agora: {abertas} tarefa(s) "
                    "aberta(s). Confere a mensagem fixada!")
        except Exception as e:
            log(f"painel sob demanda: {e}")
            tg("sendMessage", chat_id=chat_id,
               text="⚠️ Não consegui redesenhar o painel agora — "
                    "ele se atualiza sozinho em até 2 min.")
        return 0
    # busca a lista ANTES: a IA enxerga os títulos reais e responde com eles
    remotas, sha = fetch_remote()
    titulos = [t["titulo"] for t in remotas if t.get("status") == "aberta"]
    acoes = interpretar(texto, titulos_abertos=titulos,
                        contexto_reply=contexto_reply)
    novas_spec = acoes["adicionar"]
    concluir_desc = acoes["concluir"]
    cancelar_desc = acoes.get("cancelar", [])
    remarcar_spec = acoes.get("remarcar", [])
    quer_lista = acoes.get("listar", False)
    if not any([novas_spec, concluir_desc, cancelar_desc, remarcar_spec, quer_lista]):
        tg("sendMessage", chat_id=chat_id,
           text="🤔 Não identifiquei nenhuma ação nisso. Exemplos:\n"
                "• \"pagar o DAS amanhã às 9h\" (criar)\n"
                "• \"já comprei o pão\" (concluir)\n"
                "• \"a reunião com fulano foi cancelada\" (cancelar)\n"
                "• \"remarca o dentista pra sexta às 10\" (remarcar)\n"
                "• \"o que tenho pra hoje?\" (listar)")
        return 0

    agora_iso = datetime.now(TZ).isoformat(timespec="seconds")
    linhas = []
    mudou = False

    abertas = [t for t in remotas if t.get("status") == "aberta"]

    # 1) conclusões
    concluidas, nao_achadas = [], []
    for desc in concluir_desc:
        alvo = achar_tarefa(desc, abertas)
        if alvo:
            alvo["status"] = "concluida"
            alvo["atualizado_em"] = agora_iso
            concluidas.append(alvo["titulo"])
            abertas = [t for t in abertas if t["id"] != alvo["id"]]
            mudou = True
        else:
            nao_achadas.append(desc)

    # 2) cancelamentos (removem da lista de verdade)
    canceladas = []
    for desc in cancelar_desc:
        alvo = achar_tarefa(desc, abertas)
        if alvo:
            remotas = [t for t in remotas if t["id"] != alvo["id"]]
            abertas = [t for t in abertas if t["id"] != alvo["id"]]
            canceladas.append(alvo["titulo"])
            mudou = True
        else:
            nao_achadas.append(desc)

    # 3) remarcações
    remarcadas = []
    for spec in remarcar_spec:
        alvo = achar_tarefa(spec["descricao"], abertas)
        if alvo:
            alvo["prazo"] = spec["prazo"]
            alvo["atualizado_em"] = agora_iso
            alvo["notificada_local"] = False
            remarcadas.append((alvo["titulo"], spec["prazo"]))
            mudou = True
        else:
            nao_achadas.append(spec["descricao"])

    # 4) novas
    novas = [nova_tarefa(t["titulo"], t["prazo"], t["prioridade"]) for t in novas_spec]
    if novas:
        mudou = True
    final = merge_tasks(novas, remotas)
    if mudou:
        push_remote(final, sha,
                    f"bot: +{len(novas)} nova(s), {len(concluidas)} concluída(s), "
                    f"{len(canceladas)} cancelada(s), {len(remarcadas)} remarcada(s)")

    if concluidas:
        linhas.append(f"✔️ {len(concluidas)} concluída(s):")
        linhas += [f"✅ {t}" for t in concluidas]
        linhas.append("")
    if canceladas:
        linhas.append(f"🗑 {len(canceladas)} cancelada(s)/removida(s):")
        linhas += [f"✖️ {t}" for t in canceladas]
        linhas.append("")
    if remarcadas:
        linhas.append(f"📆 {len(remarcadas)} remarcada(s):")
        linhas += [f"🔁 {t} → {prazo_humano(p)}" for t, p in remarcadas]
        linhas.append("")
    if nao_achadas:
        linhas.append("🔎 Não achei nada parecido com:")
        linhas += [f"• {d}" for d in nao_achadas]
        linhas.append("")
    if novas:
        linhas.append(f"➕ {len(novas)} tarefa(s) adicionada(s):")
        for t in novas:
            linhas.append(f"{PRIO_ICON.get(t['prioridade'], '🔵')} {t['titulo']}"
                          f" — {prazo_humano(t['prazo'])}")
        linhas.append("")

    # 5) listagem sob demanda
    if quer_lista:
        pend = sorted([t for t in final if t.get("status") == "aberta"],
                      key=lambda t: (str(t.get("prazo") or "9999")))
        if pend:
            linhas.append(f"📋 Suas tarefas abertas ({len(pend)}):")
            for t in pend[:15]:
                linhas.append(f"{PRIO_ICON.get(t.get('prioridade'), '🔵')} {t['titulo']}"
                              f" — {prazo_humano(t.get('prazo'))}")
            if len(pend) > 15:
                linhas.append(f"… e mais {len(pend) - 15}")
        else:
            linhas.append("🎉 Nenhuma tarefa aberta!")
        linhas.append("")

    if mudou:
        linhas.append("📌 O painel fixado se atualiza em instantes.")
    tg("sendMessage", chat_id=chat_id, text="\n".join(linhas).strip())
    return len(novas) + len(concluidas) + len(canceladas) + len(remarcadas)


def processar_update(u, dono, st):
    m = u.get("message") or {}
    chat_id = str((m.get("chat") or {}).get("id", ""))
    if chat_id != dono:
        return  # só o Bruno manda aqui
    # se a pessoa RESPONDEU a uma mensagem do bot, o texto dela ganha contexto
    reply = (m.get("reply_to_message") or {}).get("text") or None
    try:
        if m.get("text"):
            texto = m["text"].strip()
            if texto.startswith("/"):
                if texto == "/start":
                    tg("sendMessage", chat_id=chat_id,
                       text="📋 Tarefa IA pronto! Me mande tarefas por "
                            "texto ou áudio que eu organizo.")
                return
            tg("sendChatAction", chat_id=chat_id, action="typing")
            log(f"texto recebido: {texto[:80]}" + (" [com reply]" if reply else ""))
            processar_texto(texto, chat_id, contexto_reply=reply)
        elif m.get("voice") or m.get("audio"):
            tg("sendChatAction", chat_id=chat_id, action="typing")
            arq = m.get("voice") or m.get("audio")
            audio, nome = tg_download(arq["file_id"])
            base = nome.lower()
            if base.endswith(".oga"):
                nome = nome[:-4] + ".ogg"   # Telegram manda .oga; Groq só aceita .ogg
            elif not base.endswith((".ogg", ".mp3", ".m4a", ".wav", ".opus",
                                    ".flac", ".webm", ".mp4", ".mpga", ".mpeg")):
                nome = "audio.ogg"
            log(f"áudio recebido ({len(audio)} bytes)")
            texto = transcrever(audio, nome)
            if not texto:
                tg("sendMessage", chat_id=chat_id,
                   text="🤔 Não consegui entender o áudio. Tenta de novo?")
                return
            processar_texto(texto, chat_id, contexto_reply=reply)
    except Exception as e:
        detalhe = ""
        resp = getattr(e, "response", None)
        if resp is not None:
            detalhe = f" | corpo: {resp.text[:300]}"
        log(f"erro processando update: {e}{detalhe}")
        try:
            tg("sendMessage", chat_id=chat_id,
               text="⚠️ Deu um erro ao processar sua mensagem. "
                    "Tenta de novo em instantes.")
        except Exception:
            pass


def main():
    if not GROQ_KEY_FILE.exists():
        log("sem chave Groq (/root/.groq-tarefa-key) — ouvinte inativo")
        return 0
    dono = TG_CHATID_FILE.read_text().strip()
    st = load_state()
    log("plantonista iniciado — escuta contínua ativa")

    # Plantonista: grudado na porta do Telegram para sempre.
    erros_seguidos = 0
    while True:
        try:
            updates = tg("getUpdates", timeout_http=POLL_SECONDS + 10,
                         offset=st.get("offset", 0) + 1,
                         timeout=POLL_SECONDS) or []
            erros_seguidos = 0
        except Exception as e:
            erros_seguidos += 1
            log(f"getUpdates falhou ({erros_seguidos}x): {e}")
            if erros_seguidos >= 10:
                log("muitas falhas seguidas — encerrando (vigia reinicia)")
                save_state(st)
                return 1
            time.sleep(min(3 * erros_seguidos, 30))
            continue
        for u in updates:
            st["offset"] = max(st.get("offset", 0), u.get("update_id", 0))
            processar_update(u, dono, st)
        if updates:
            save_state(st)


if __name__ == "__main__":
    sys.exit(main())
