<?php

declare(strict_types=1);

function mca_setup_dispatch(string $method): never
{
    if ($method === 'GET') {
        mca_setup_form();
    }

    if ($method !== 'POST') {
        header('Allow: GET, POST');
        throw new McaHttpException(405, 'method_not_allowed', 'Only GET and POST are allowed.');
    }

    mca_setup_submit();
}

function mca_setup_requirements(): array
{
    return [
        'PHP 8.2+' => version_compare(PHP_VERSION, '8.2.0', '>='),
        'PDO MySQL' => extension_loaded('pdo_mysql'),
        'Sodium' => extension_loaded('sodium') && function_exists('sodium_crypto_sign_keypair'),
        'JSON' => extension_loaded('json'),
        'Random bytes' => function_exists('random_bytes'),
    ];
}

function mca_setup_form(array $errors = [], array $values = []): never
{
    $requirements = mca_setup_requirements();
    $requirementsOk = !in_array(false, $requirements, true);
    $detectedHost = strtolower((string) ($_SERVER['HTTP_HOST'] ?? 'mcattach.elektroniker.help'));
    $detectedHost = preg_replace('/:\d+$/D', '', $detectedHost) ?? $detectedHost;

    $defaults = [
        'base_url' => 'https://' . $detectedHost,
        'db_host' => 'localhost',
        'db_port' => '3306',
        'db_name' => '',
        'db_user' => '',
    ];
    $values = array_merge($defaults, $values);

    $checks = '';
    foreach ($requirements as $name => $ok) {
        $checks .= '<li class="' . ($ok ? 'ok' : 'error') . '">'
            . ($ok ? 'OK - ' : 'НЕТ - ') . mca_escape($name) . '</li>';
    }

    $errorHtml = '';
    if ($errors !== []) {
        $errorHtml .= '<div class="error"><strong>Установка не выполнена:</strong><ul>';
        foreach ($errors as $error) {
            $errorHtml .= '<li>' . mca_escape((string) $error) . '</li>';
        }
        $errorHtml .= '</ul></div>';
    }

    $disabled = $requirementsOk ? '' : ' disabled';
    $content = '<p>Одноразовая установка MCAttach Relay 0.1.0.</p>'
        . '<p class="muted">Сначала создай отдельную базу MySQL в hPanel. Пароль базы сохраняется вне public_html.</p>'
        . '<h2>Проверка сервера</h2><ul>' . $checks . '</ul>' . $errorHtml
        . '<form method="post" action="/setup" autocomplete="off">'
        . '<label for="setup_key">Ключ установки из SETUP_KEY.txt</label>'
        . '<input id="setup_key" name="setup_key" type="password" required maxlength="128">'
        . '<label for="base_url">Публичный адрес Relay</label>'
        . '<input id="base_url" name="base_url" type="url" required value="' . mca_escape((string) $values['base_url']) . '">'
        . '<div class="grid"><div><label for="db_host">Сервер MySQL</label>'
        . '<input id="db_host" name="db_host" required value="' . mca_escape((string) $values['db_host']) . '"></div>'
        . '<div><label for="db_port">Порт MySQL</label>'
        . '<input id="db_port" name="db_port" inputmode="numeric" required value="' . mca_escape((string) $values['db_port']) . '"></div></div>'
        . '<label for="db_name">Имя базы данных</label>'
        . '<input id="db_name" name="db_name" required value="' . mca_escape((string) $values['db_name']) . '">'
        . '<label for="db_user">Пользователь базы данных</label>'
        . '<input id="db_user" name="db_user" required value="' . mca_escape((string) $values['db_user']) . '">'
        . '<label for="db_password">Пароль базы данных</label>'
        . '<input id="db_password" name="db_password" type="password" required maxlength="512">'
        . '<button type="submit"' . $disabled . '>Установить Relay</button></form>'
        . '<p class="muted">После успешной установки будет показан единственный upload access token. Сохрани его сразу.</p>';

    mca_html(mca_page('Установка MCAttach Relay', $content), $errors === [] ? 200 : 400);
}

function mca_setup_submit(): never
{
    $requirements = mca_setup_requirements();
    if (in_array(false, $requirements, true)) {
        mca_setup_form(['На сервере отсутствуют обязательные расширения PHP.']);
    }

    $values = [
        'base_url' => trim((string) ($_POST['base_url'] ?? '')),
        'db_host' => trim((string) ($_POST['db_host'] ?? '')),
        'db_port' => trim((string) ($_POST['db_port'] ?? '')),
        'db_name' => trim((string) ($_POST['db_name'] ?? '')),
        'db_user' => trim((string) ($_POST['db_user'] ?? '')),
    ];
    $password = (string) ($_POST['db_password'] ?? '');
    $submittedKey = trim((string) ($_POST['setup_key'] ?? ''));
    $errors = [];

    $keyFile = MCA_PUBLIC_ROOT . '/SETUP_KEY.txt';
    $expectedKey = is_file($keyFile) ? trim((string) file_get_contents($keyFile)) : '';
    if ($expectedKey === '' || !hash_equals($expectedKey, $submittedKey)) {
        usleep(750000);
        $errors[] = 'Неверный ключ установки.';
    }

    $url = parse_url($values['base_url']);
    if (!is_array($url)
        || strtolower((string) ($url['scheme'] ?? '')) !== 'https'
        || ($url['host'] ?? '') === ''
        || isset($url['user'])
        || isset($url['pass'])
        || (($url['path'] ?? '') !== '' && ($url['path'] ?? '') !== '/')
        || isset($url['query'])
        || isset($url['fragment'])
    ) {
        $errors[] = 'Публичный адрес должен быть HTTPS origin без пути, например https://mcattach.elektroniker.help.';
    }

    if (preg_match('/^[A-Za-z0-9._-]{1,253}$/D', $values['db_host']) !== 1) {
        $errors[] = 'Некорректное имя сервера MySQL.';
    }
    $port = filter_var($values['db_port'], FILTER_VALIDATE_INT, ['options' => ['min_range' => 1, 'max_range' => 65535]]);
    if ($port === false) {
        $errors[] = 'Некорректный порт MySQL.';
    }
    if (preg_match('/^[A-Za-z0-9_$.-]{1,64}$/D', $values['db_name']) !== 1) {
        $errors[] = 'Некорректное имя базы данных.';
    }
    if (preg_match('/^[A-Za-z0-9_$.-]{1,64}$/D', $values['db_user']) !== 1) {
        $errors[] = 'Некорректное имя пользователя базы данных.';
    }
    if ($password === '' || strlen($password) > 512) {
        $errors[] = 'Пароль базы данных отсутствует или слишком длинный.';
    }

    if ($errors !== []) {
        mca_setup_form($errors, $values);
    }

    $normalizedBaseUrl = strtolower(rtrim($values['base_url'], '/'));
    $dsn = sprintf(
        'mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4',
        $values['db_host'],
        (int) $port,
        $values['db_name']
    );

    try {
        $pdo = new PDO($dsn, $values['db_user'], $password, [
            PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
            PDO::ATTR_EMULATE_PREPARES => false,
        ]);
        $pdo->exec("SET time_zone = '+00:00'");
        require_once MCA_PUBLIC_ROOT . '/src/database.php';
        mca_install_schema($pdo);
    } catch (Throwable $error) {
        error_log('MCAttach Relay setup database failure: ' . get_class($error));
        mca_setup_form([
            'Не удалось подключиться к MySQL или создать таблицы. Проверь имя базы, пользователя, пароль и сервер.',
        ], $values);
    }

    if (!is_dir(MCA_PRIVATE_ROOT) && !mkdir(MCA_PRIVATE_ROOT, 0700, true) && !is_dir(MCA_PRIVATE_ROOT)) {
        mca_setup_form(['Не удалось создать приватный каталог рядом с public_html.'], $values);
    }

    $objectsRoot = MCA_PRIVATE_ROOT . '/objects';
    if (!is_dir($objectsRoot) && !mkdir($objectsRoot, 0700, true) && !is_dir($objectsRoot)) {
        mca_setup_form(['Не удалось создать приватное объектное хранилище.'], $values);
    }

    @file_put_contents(
        MCA_PRIVATE_ROOT . '/.htaccess',
        "Deny from all\n<IfModule mod_authz_core.c>\nRequire all denied\n</IfModule>\n",
        LOCK_EX
    );

    $keypair = sodium_crypto_sign_keypair();
    $publicKey = sodium_crypto_sign_publickey($keypair);
    $secretKey = sodium_crypto_sign_secretkey($keypair);
    $uploadAccessToken = 'mca_up_' . mca_base64url_encode(random_bytes(32));

    $config = [
        'app_version' => MCA_RELAY_VERSION,
        'base_url' => $normalizedBaseUrl,
        'database' => [
            'host' => $values['db_host'],
            'port' => (int) $port,
            'name' => $values['db_name'],
            'user' => $values['db_user'],
            'password' => $password,
        ],
        'service_public_key' => mca_base64url_encode($publicKey),
        'service_secret_key' => mca_base64url_encode($secretKey),
        'upload_access_token_hash' => hash('sha256', $uploadAccessToken),
        'rate_limit_secret' => mca_base64url_encode(random_bytes(32)),
        'limits' => [
            'max_ciphertext_bytes' => 6291456,
            'max_manifest_bytes' => 262144,
            'max_chunk_bytes' => 307200,
            'max_chunks' => 64,
            'max_receipts' => 32,
            'min_hard_ttl_seconds' => 3600,
            'default_hard_ttl_seconds' => 259200,
            'max_hard_ttl_seconds' => 259200,
            'default_grace_seconds' => 3600,
            'max_grace_seconds' => 86400,
            'upload_session_seconds' => 21600,
            'tombstone_seconds' => 604800,
            'max_storage_bytes' => 10737418240,
        ],
    ];

    $configSource = "<?php\n\ndeclare(strict_types=1);\n\nreturn "
        . var_export($config, true) . ";\n";
    $temporaryConfig = MCA_CONFIG_FILE . '.tmp-' . bin2hex(random_bytes(6));

    if (file_put_contents($temporaryConfig, $configSource, LOCK_EX) === false) {
        mca_setup_form(['Не удалось записать конфигурацию Relay.'], $values);
    }
    @chmod($temporaryConfig, 0600);
    if (!rename($temporaryConfig, MCA_CONFIG_FILE)) {
        @unlink($temporaryConfig);
        mca_setup_form(['Не удалось активировать конфигурацию Relay.'], $values);
    }

    @unlink($keyFile);

    $providerId = mca_provider_id($normalizedBaseUrl, $config['service_public_key']);
    $content = '<p class="ok"><strong>Relay установлен.</strong></p>'
        . '<p>Сохрани следующий upload access token. После ухода со страницы он больше не показывается:</p>'
        . '<pre>' . mca_escape($uploadAccessToken) . '</pre>'
        . '<p>Provider ID:</p><pre>' . mca_escape($providerId) . '</pre>'
        . '<p>Service public key:</p><pre>' . mca_escape($config['service_public_key']) . '</pre>'
        . '<p><a class="button" href="/">Открыть статус Relay</a></p>';
    mca_html(mca_page('MCAttach Relay установлен', $content));
}
