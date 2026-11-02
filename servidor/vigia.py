# -*- coding: utf-8 -*-
"""
Vigia do Tarefa IA — roda no servidor do Bob (cron, a cada 5 min).
Lê as tarefas do repositório privado (sincronizadas pelo app no PC do Bruno),
envia lembretes no canal Telegram "Tarefa IA" na hora certa e o resumo
matinal às 07:45 (horário de Brasília). Nunca envia o mesmo aviso duas vezes.
"""

import json
import sys
from base64 import b64decode
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("America/Sao_Paulo")
REPO = "brunohpr-hash/tarefa-ia-dados"
GH_TOKEN_FILE = Path("/root/.github-ditado-token")
TG_TOKEN_FILE = Path("/root/.telegram-tarefa-token")
TG_CHATID_FILE = Path("/root/.telegram-tarefa-chatid")
STATE_PATH = Path(__file__).resolve().parent / "estado.json"

HORA_RESUMO = (7, 45)          # 07:45 Brasília
JANELA_RESUMO_MIN = 30         # envia até 08:15 se o cron atrasar
HORA_PADRAO_DIA = (9, 0)       # tarefa só com data avisa às 09:00
PRIO_ICON = {"alta": "🔴", "normal": "🔵", "baixa": "⚪"}
DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def log(msg):
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}")


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"avisadas": [], "resumo_enviado_em": ""}


def save_state(st):
    st["avisadas"] = st.get("avisadas", [])[-2000:]
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_tasks():
    token = GH_TOKEN_FILE.read_text().strip()
    r = requests.get(
        f"https://api.github.com/repos/{REPO}/contents/tarefas.json",
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github+json"},
        timeout=20)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    data = json.loads(b64decode(r.json()["content"]).decode("utf-8"))
    return data.get("tarefas", [])


def send_telegram(text):
    token = TG_TOKEN_FILE.read_text().strip()
    chat_id = TG_CHATID_FILE.read_text().strip()
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=20)
    r.raise_for_status()


def parse_prazo(prazo):
    if not prazo:
        return None, False
    s = str(prazo).strip()
    for fmt, com_hora in (("%Y-%m-%d %H:%M", True), ("%Y-%m-%d", False)):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=TZ), com_hora
        except Exception:
            continue
    return None, False


def momento_do_aviso(prazo):
    dt, com_hora = parse_prazo(prazo)
    if dt is None:
        return None
    if not com_hora:
        dt = dt.replace(hour=HORA_PADRAO_DIA[0], minute=HORA_PADRAO_DIA[1])
    return dt


def prazo_humano(prazo, agora):
    dt, com_hora = parse_prazo(prazo)
    if not dt:
        return "sem prazo"
    hoje = agora.date()
    if dt.date() == hoje:
        dia = "hoje"
    elif dt.date() == hoje + timedelta(days=1):
        dia = "amanhã"
    elif dt.date() < hoje:
        dia = f"{dt:%d/%m} (ATRASADA)"
    else:
        dia = f"{dt:%d/%m} ({DIAS[dt.weekday()]})" if (dt.date() - hoje).days < 7 else f"{dt:%d/%m}"
    return f"{dia} às {dt:%H:%M}" if com_hora else dia


def tarefas_a_avisar(tarefas, agora, avisadas):
    """Tarefas abertas cujo momento de aviso chegou e ainda não foram avisadas."""
    out = []
    for t in tarefas:
        if t.get("status") != "aberta" or t.get("id") in avisadas:
            continue
        dt = momento_do_aviso(t.get("prazo"))
        if dt is None:
            continue
        # evita spam de tarefas atrasadas há dias: só avisa até 48h depois
        if dt <= agora <= dt + timedelta(hours=48):
            out.append(t)
    return out


def montar_resumo(tarefas, agora):
    hoje = agora.date()
    abertas = [t for t in tarefas if t.get("status") == "aberta"]
    do_dia, atrasadas = [], []
    for t in abertas:
        dt, _ = parse_prazo(t.get("prazo"))
        if dt is None:
            continue
        if dt.date() == hoje:
            do_dia.append(t)
        elif dt.date() < hoje:
            atrasadas.append(t)
    if not do_dia and not atrasadas:
        return None
    linhas = [f"☀️ Bom dia! Suas tarefas — {agora:%d/%m} ({DIAS[agora.weekday()]}):", ""]
    for t in sorted(do_dia, key=lambda x: str(x.get("prazo"))):
        linhas.append(f"{PRIO_ICON.get(t.get('prioridade'), '🔵')} {t['titulo']}"
                      f" — {prazo_humano(t.get('prazo'), agora)}")
    if atrasadas:
        linhas += ["", "⚠️ Atrasadas:"]
        for t in sorted(atrasadas, key=lambda x: str(x.get("prazo")))[:10]:
            linhas.append(f"• {t['titulo']} — {prazo_humano(t.get('prazo'), agora)}")
    linhas += ["", "✅ Para concluir ou adiar, use o app Tarefa IA no PC."]
    return "\n".join(linhas)


def main():
    agora = datetime.now(TZ)
    st = load_state()
    try:
        tarefas = fetch_tasks()
    except Exception as e:
        log(f"erro buscando tarefas: {e}")
        return 1

    # 1) lembretes na hora certa
    for t in tarefas_a_avisar(tarefas, agora, set(st.get("avisadas", []))):
        msg = (f"⏰ Lembrete: {PRIO_ICON.get(t.get('prioridade'), '🔵')} {t['titulo']}\n"
               f"Prazo: {prazo_humano(t.get('prazo'), agora)}")
        try:
            send_telegram(msg)
            st.setdefault("avisadas", []).append(t["id"])
            log(f"lembrete enviado: {t['titulo']}")
        except Exception as e:
            log(f"erro enviando lembrete: {e}")

    # 2) resumo matinal (07:45, janela de 30 min, 1x por dia)
    inicio = agora.replace(hour=HORA_RESUMO[0], minute=HORA_RESUMO[1],
                           second=0, microsecond=0)
    if inicio <= agora <= inicio + timedelta(minutes=JANELA_RESUMO_MIN):
        if st.get("resumo_enviado_em") != str(agora.date()):
            resumo = montar_resumo(tarefas, agora)
            if resumo:
                try:
                    send_telegram(resumo)
                    log("resumo matinal enviado")
                except Exception as e:
                    log(f"erro no resumo: {e}")
            st["resumo_enviado_em"] = str(agora.date())

    save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
