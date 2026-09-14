# MCAttach Relay HTTP API 0.1.0

Base URL:

```text
https://mcattach.elektroniker.help
```

Все даты возвращаются в UTC. JSON использует UTF-8. Binary upload выполняется
как `application/octet-stream`. Токены передаются только заголовком:

```http
Authorization: Bearer TOKEN
```

Для окружений, удаляющих `Authorization`, поддерживается эквивалентный
заголовок `X-MCA-Token: Bearer TOKEN`.

## Public information

### GET /health

Проверяет PHP и соединение с MySQL.

### GET /v1/info

Возвращает версию протокола, `provider_id`, публичный Ed25519 service key,
лимиты и возможности Relay. Полный service key должен быть закреплён в
доверенном provider profile MeshCenter.

## Создание upload session

### POST /v1/uploads

Требует главный upload access token.

Пример тела:

```json
{
  "transfer_id": "AAAAAAAAAAAAAAAAAAAAAA",
  "total_size": 6,
  "ciphertext_sha256": "bef57ec7f53a6d40beb640a780a639c83bc29ac8a9816f1fc6c5c6dcd93c4721",
  "manifest_size": 4,
  "manifest_sha256": "9f64a747e1b97f131fabb6b447296c9b6f0201e79fb3c5356e6c77e89b6a806a",
  "chunks": [
    {
      "size": 3,
      "sha256": "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    },
    {
      "size": 3,
      "sha256": "cb8379ac2098aa165029e3938a51da0bcecfc008fd6795f401178647f96c5b34"
    }
  ],
  "receipt_hashes": [
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  ],
  "hard_ttl_seconds": 259200,
  "download_grace_seconds": 3600
}
```

`transfer_id` - Base64URL без padding от 16 случайных байт. SHA-256 значения
используют lowercase hex. `receipt_hashes` содержат SHA-256 от индивидуальных
32-byte receipt secrets, а не сами secrets.

Ответ `201` содержит случайные:

- `upload_id`;
- `upload_token` для chunks, manifest и commit;
- `revoke_token` для удаления объекта.

MeshCenter обязан сохранить их в защищённой локальной очереди. Relay хранит
только hashes токенов.

`POST /v1/uploads` не является идемпотентным. MeshCenter должен сохранить
ответ до отправки частей. Если ответ был полностью потерян из-за разрыва
соединения, клиент создаёт новый `transfer_id`; незавершённая старая сессия
будет автоматически удалена через 6 часов.

### GET /v1/uploads/{upload_id}

Требует session `upload_token`. Возвращает состояние сессии, признак принятого
manifest и список chunks с полем `uploaded`. MeshCenter использует endpoint
после перезапуска или восстановления сети и повторно отправляет только
отсутствующие части. Повторная отправка части с тем же digest допустима.

## Загрузка данных

### PUT /v1/uploads/{upload_id}/chunks/{index}

Требует session `upload_token`. Тело должно точно совпасть с размером и digest,
заявленными при создании session. Повторная загрузка тех же байтов идемпотентна.

### PUT /v1/uploads/{upload_id}/manifest

Требует session `upload_token`. Тело - непрозрачный encrypted manifest bundle.

### POST /v1/uploads/{upload_id}/commit

Требует session `upload_token`. Relay повторно проверяет manifest, наличие всех
chunks, общий размер и SHA-256 упорядоченного ciphertext. До успешного commit
объект не доступен получателю.

## Получение

### GET /v1/objects/{transfer_id}/descriptor

Возвращает:

- public Relay descriptor;
- encrypted manifest в Base64URL;
- Ed25519 service signature над canonical descriptor.

Подпись Relay не заменяет sender descriptor signature внутри MCA bundle.
Клиент обязан проверить обе независимые подписи согласно MCA/1.

### GET /v1/objects/{transfer_id}/chunks/{index}

Возвращает ciphertext chunk как `application/octet-stream` с размером и
SHA-256 в заголовках.

### POST /v1/objects/{transfer_id}/complete

Вызывается только после полной загрузки, проверки AEAD, digest, sender signature
и создания локального temporary file.

```json
{
  "receipt_secret": "BASE64URL_OF_32_RANDOM_BYTES"
}
```

После подтверждения всех заявленных recipients начинается download grace.
Повторный запрос с тем же secret идемпотентен.

## Отзыв

### DELETE /v1/objects/{transfer_id}

Требует `revoke_token`. Ciphertext удаляется, а короткая tombstone-запись
сохраняется 7 дней.

## Основные HTTP-коды

- `200` - операция выполнена;
- `201` - upload session создана;
- `400` - некорректный transport format;
- `401` - отсутствует или неверен токен;
- `403` - неверное receipt proof;
- `404` - endpoint/session/object не найден;
- `409` - конфликт состояния или неполный upload;
- `410` - объект истёк либо отозван;
- `413` - превышен размер request;
- `422` - размер, digest или поля не совпадают;
- `429` - rate limit;
- `507` - внутренняя квота Relay исчерпана.
