#!/usr/bin/env bash
# start_davinci.sh — sobe o stack DaVinci localmente:
#   Postgres + Redis (docker-compose), migrations, Django (8001),
#   Celery worker e Celery beat.
# Portas escolhidas para NÃO colidir com o projeto AllLife (Django 8000 / Postgres 5434 / Redis 6379).
# Local-only — não usar em produção. Ctrl+C encerra tudo.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_ACTIVATE="$REPO_ROOT/.venv/bin/activate"
COMPOSE_FILE="$REPO_ROOT/docker-compose.yml"
FRONTEND_DIR="$REPO_ROOT/davinci-frontend"

# ── Portas (sem conflito com AllLife) ──────────────────────────────────────────
DJANGO_PORT=8001   # AllLife usa 8000
PG_PORT=5435       # AllLife usa 5434  (mapeado no docker-compose.yml)
REDIS_PORT=6380    # AllLife usa 6379  (mapeado no docker-compose.yml)
FRONTEND_PORT=3000 # Next.js dev

# ── docker compose v2 com fallback para docker-compose v1 ──────────────────────
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  DC=(docker-compose)
else
  echo "ERRO: docker compose não encontrado. Instale o Docker Desktop."
  exit 1
fi

# ── venv ───────────────────────────────────────────────────────────────────────
if [ ! -f "$VENV_ACTIVATE" ]; then
  echo "ERRO: venv não encontrado em $VENV_ACTIVATE"
  echo "Crie com: python3 -m venv $REPO_ROOT/.venv && source $VENV_ACTIVATE && pip install -r $REPO_ROOT/requirements.txt"
  exit 1
fi
# shellcheck disable=SC1090
source "$VENV_ACTIVATE"

# ── Infra: Postgres + Redis ────────────────────────────────────────────────────
echo "==> Subindo Postgres + Redis (docker-compose)..."
"${DC[@]}" -f "$COMPOSE_FILE" up -d

echo "==> Aguardando Postgres em localhost:$PG_PORT..."
for _ in $(seq 1 30); do
  if "${DC[@]}" -f "$COMPOSE_FILE" exec -T db pg_isready -U davinci >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

cd "$REPO_ROOT"

# ── Migrations ─────────────────────────────────────────────────────────────────
echo "==> Aplicando migrations..."
python manage.py migrate --noinput

# ── Processos da aplicação (Django + Celery worker/beat + Next.js) ─────────────
PIDS=()

# Mata um processo e toda a sua árvore de filhos (celery prefork, npm→next, etc.).
kill_tree() {
  local pid=$1 child
  for child in $(pgrep -P "$pid" 2>/dev/null); do
    kill_tree "$child"
  done
  kill "$pid" 2>/dev/null || true
}

start_apps() {
  echo "==> Celery worker..."
  celery -A config worker -l info &
  PIDS+=($!)

  echo "==> Celery beat..."
  celery -A config beat -l info &
  PIDS+=($!)

  echo "==> Django runserver em http://localhost:$DJANGO_PORT ..."
  python manage.py runserver "0.0.0.0:$DJANGO_PORT" &
  PIDS+=($!)

  if [ -d "$FRONTEND_DIR/node_modules" ]; then
    echo "==> Next.js dev em http://localhost:$FRONTEND_PORT ..."
    ( cd "$FRONTEND_DIR" && npm run dev -- --port "$FRONTEND_PORT" ) &
    PIDS+=($!)
  else
    echo "AVISO: $FRONTEND_DIR/node_modules ausente — pulando frontend."
    echo "       Rode 'cd davinci-frontend && npm install' e reinicie."
  fi
}

stop_apps() {
  for pid in "${PIDS[@]:-}"; do
    kill_tree "$pid"
  done
  # Aguarda os processos efetivamente morrerem (libera as portas antes do restart).
  for pid in "${PIDS[@]:-}"; do
    wait "$pid" 2>/dev/null || true
  done
  PIDS=()
}

banner() {
  echo ""
  echo "──────────────────────────────────────────────"
  echo " DaVinci no ar:"
  echo "   Frontend   : http://localhost:$FRONTEND_PORT"
  echo "   API Django : http://localhost:$DJANGO_PORT/api/v1"
  echo "   Postgres   : localhost:$PG_PORT  (db: davinci_db)"
  echo "   Redis      : localhost:$REDIS_PORT"
  echo "──────────────────────────────────────────────"
  echo " Teclas:  [Shift+R] reiniciar apps   [Q] sair   (Ctrl+C também sai)"
  echo "          A infra (Postgres+Redis) permanece de pé no restart."
  echo "──────────────────────────────────────────────"
  echo ""
}

cleanup() {
  echo ""
  echo "==> Encerrando processos da aplicação..."
  stop_apps
}
trap cleanup EXIT INT TERM

start_apps
banner

# ── Loop de teclas (só em terminal interativo; senão, apenas aguarda) ──────────
if [ -t 0 ]; then
  while true; do
    # -rsn1: lê 1 tecla, sem echo. '|| true' evita que set -e derrube no EOF.
    read -rsn1 key || { sleep 0.5; continue; }
    case "$key" in
      R)  # Shift+R = R maiúsculo
        echo "==> Reiniciando apps..."
        stop_apps
        start_apps
        banner
        ;;
      q|Q)
        break
        ;;
      h|H|'?')
        banner
        ;;
    esac
  done
else
  wait
fi
