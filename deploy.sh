#!/usr/bin/env bash
#
# Usage: ./deploy.sh <dev|prod> "commit message"

set -e

ENVIRONMENT="$1"
COMMIT_MESSAGE="$2"

REMOTE_USER="flint"
REMOTE_PATH="/home/flint/meshcenter"

if [ "$ENVIRONMENT" = "dev" ]; then
    REMOTE_HOST="192.168.2.104"
elif [ "$ENVIRONMENT" = "prod" ]; then
    REMOTE_HOST="192.168.2.103"
else
    echo "Usage: $0 <dev|prod> \"commit message\""
    exit 1
fi

if [ -z "$COMMIT_MESSAGE" ]; then
    echo "Error: commit message is required."
    echo "Usage: $0 <dev|prod> \"commit message\""
    exit 1
fi

echo "==> Target environment: $ENVIRONMENT ($REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH)"

echo "==> Staging changes (git add .)"
git add .

echo "==> Committing changes"
COMMIT_OUTPUT=$(git commit -m "$COMMIT_MESSAGE" 2>&1) && COMMIT_STATUS=0 || COMMIT_STATUS=$?
echo "$COMMIT_OUTPUT"

if [ "$COMMIT_STATUS" -ne 0 ]; then
    if echo "$COMMIT_OUTPUT" | grep -q "nothing to commit"; then
        echo "Нет новых изменений для коммита, деплою текущую версию из GitHub"
    else
        echo "Error: git commit failed"
        exit 1
    fi
fi

echo "==> Pushing to remote"
git push

echo "==> Deploying to $REMOTE_HOST"
ssh "$REMOTE_USER@$REMOTE_HOST" "cd $REMOTE_PATH && git pull && sudo systemctl restart meshcenter.service"

echo "==> Deployment to $ENVIRONMENT complete"
