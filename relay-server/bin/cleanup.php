<?php

declare(strict_types=1);

if (PHP_SAPI !== 'cli') {
    http_response_code(404);
    exit;
}

define('MCA_RELAY_VERSION', '0.1.0');
define('MCA_PUBLIC_ROOT', dirname(__DIR__));
define('MCA_PRIVATE_ROOT', dirname(MCA_PUBLIC_ROOT) . DIRECTORY_SEPARATOR . 'mcattach_private');
define('MCA_CONFIG_FILE', MCA_PRIVATE_ROOT . DIRECTORY_SEPARATOR . 'config.php');

require MCA_PUBLIC_ROOT . '/src/common.php';
require MCA_PUBLIC_ROOT . '/src/database.php';
require MCA_PUBLIC_ROOT . '/src/storage.php';
require MCA_PUBLIC_ROOT . '/src/maintenance.php';

if (!is_file(MCA_CONFIG_FILE)) {
    fwrite(STDERR, "MCAttach Relay is not installed.\n");
    exit(2);
}

$config = require MCA_CONFIG_FILE;
if (!is_array($config)) {
    fwrite(STDERR, "Invalid MCAttach Relay configuration.\n");
    exit(2);
}

try {
    $result = mca_cleanup(mca_database($config), $config, 500);
    fwrite(STDOUT, json_encode($result, JSON_UNESCAPED_SLASHES | JSON_THROW_ON_ERROR) . PHP_EOL);
} catch (Throwable $error) {
    fwrite(STDERR, 'Cleanup failed: ' . get_class($error) . PHP_EOL);
    exit(1);
}

