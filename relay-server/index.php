<?php

declare(strict_types=1);

define('MCA_RELAY_VERSION', '0.1.0');
define('MCA_PUBLIC_ROOT', __DIR__);
define('MCA_PRIVATE_ROOT', dirname(__DIR__) . DIRECTORY_SEPARATOR . 'mcattach_private');
define('MCA_CONFIG_FILE', MCA_PRIVATE_ROOT . DIRECTORY_SEPARATOR . 'config.php');

require MCA_PUBLIC_ROOT . '/src/common.php';

mca_install_exception_handler();

$path = mca_request_path();
$method = strtoupper((string) ($_SERVER['REQUEST_METHOD'] ?? 'GET'));

if (!is_file(MCA_CONFIG_FILE)) {
    if ($path === '/setup') {
        if (!mca_is_https()) {
            throw new McaHttpException(400, 'https_required', 'Setup is available only over HTTPS.');
        }
        require MCA_PUBLIC_ROOT . '/src/setup.php';
        mca_setup_dispatch($method);
        exit;
    }

    if ($path === '/health') {
        mca_json([
            'ok' => false,
            'status' => 'setup_required',
            'version' => MCA_RELAY_VERSION,
        ], 503);
    }

    if ($path === '/' && $method === 'GET') {
        mca_setup_required_page();
        exit;
    }

    throw new McaHttpException(503, 'setup_required', 'Relay setup has not been completed.');
}

$config = require MCA_CONFIG_FILE;
if (!is_array($config)) {
    throw new RuntimeException('Invalid Relay configuration.');
}

require MCA_PUBLIC_ROOT . '/src/database.php';
require MCA_PUBLIC_ROOT . '/src/relay.php';

mca_require_configured_host($config);
mca_relay_dispatch($config, $method, $path);
