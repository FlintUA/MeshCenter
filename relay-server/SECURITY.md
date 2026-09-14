# Security policy - MCAttach Relay 0.1.0

## Статус

Это pilot build для частного тестирования. Он не проходил независимый аудит и
не должен предлагаться как публичное неограниченное файловое хранилище.

## Модель данных

Relay принимает только ciphertext. Клиентская реализация MeshCenter отвечает
за XChaCha20-Poly1305, recipient envelopes, sender signatures и проверку файла.
Ed25519 service signature Relay подтверждает ответ конкретного закреплённого
Relay, но не авторство пользовательского файла.

Relay всё равно видит технические метаданные:

- IP-адрес через журналы веб-хостинга;
- время upload/download;
- точный размер ciphertext;
- transfer ID;
- количество receipt hashes.

Эти данные нельзя автоматически считать анонимными.

## Реализованные меры

- HTTPS обязателен;
- anonymous upload выключен;
- главный upload token и отдельные session/revoke tokens;
- в MySQL сохраняются только SHA-256 hashes токенов;
- transfer ID содержит 128 бит случайности;
- все object paths формируются сервером;
- строгие размеры и SHA-256 каждого chunk и всего ciphertext;
- объект невидим до атомарного commit состояния;
- rate limits по HMAC-псевдонимизированному IP;
- внутренняя quota;
- hard expiry, completion grace и tombstones;
- конфигурация и ciphertext вне `public_html`;
- запрет directory listing и прямого HTTP-доступа к source/maintenance files;
- `nosniff`, CSP, frame denial, no-store и HSTS на HTTPS;
- отсутствие исходных имён файлов и MIME в открытом виде;
- отсутствие сторонних runtime dependencies.

## Перед публичным запуском

Обязательны:

1. независимый review PHP-кода и MCA/1 crypto format;
2. интеграционные и нагрузочные тесты на фактическом тарифе;
3. проверка backup и гарантированного удаления;
4. privacy notice и retention policy;
5. acceptable use и abuse response;
6. ротация upload access token;
7. мониторинг quota, rate limits, ошибок и свободного места;
8. решение о registration/account tokens вместо одного общего upload token;
9. проверка журналов Hostinger и их срока хранения;
10. проверка DPA, страны обработки и применимых требований GDPR.

## Сообщение об уязвимости

Не публикуй рабочие токены, transfer IDs или ciphertext в открытом issue.
Используй приватный канал владельца проекта.

