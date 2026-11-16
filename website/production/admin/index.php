<?php
declare(strict_types=1);

ini_set('display_errors', '0');
header('Content-Type: text/html; charset=utf-8');
header('Cache-Control: private, no-store, max-age=0');
header('X-Robots-Tag: noindex, nofollow');
header('X-Content-Type-Options: nosniff');
header('Referrer-Policy: no-referrer');
header("Content-Security-Policy: default-src 'none'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'");

require __DIR__ . '/statistics.php';

function escape(string $value): string
{
    return htmlspecialchars($value, ENT_QUOTES | ENT_SUBSTITUTE, 'UTF-8');
}

function number(int $value): string
{
    return number_format($value, 0, ',', ' ');
}

function bytesLabel(int $bytes): string
{
    $value = $bytes;
    foreach (['Б', 'КиБ', 'МиБ', 'ГиБ', 'ТиБ'] as $unit) {
        if ($value < 1024 || $unit === 'ТиБ') {
            return number_format($value, $unit === 'Б' ? 0 : 1, ',', ' ') . ' ' . $unit;
        }
        $value /= 1024;
    }
}

function chart(array $daily, string $metric, string $label): void
{
    if (!$daily) {
        echo '<p class="muted">За этот период нет записей.</p>';
        return;
    }
    $peak = max(array_column($daily, $metric));
    $step = 480 / count($daily);
    ?>
    <div class="chart-scale"><span><?= number($peak) ?></span><span>0</span></div>
    <svg class="chart <?= escape($metric) ?>" viewBox="0 0 480 112" preserveAspectRatio="none" role="img" aria-label="<?= escape($label) ?> по дням">
        <path class="grid" d="M0 4H480 M0 57H480 M0 110H480"/>
        <?php $offset = 0; foreach ($daily as $day => $values):
            $height = $peak ? round(106 * $values[$metric] / $peak, 2) : 0;
            ?>
            <rect x="<?= round($offset + $step * 0.13, 2) ?>" y="<?= 110 - $height ?>" width="<?= round($step * 0.74, 2) ?>" height="<?= $height ?>" rx="2">
                <title><?= escape($day) ?> · <?= number($values[$metric]) ?></title>
            </rect>
        <?php $offset += $step; endforeach; ?>
    </svg>
    <div class="chart-dates"><span><?= escape(date('d.m', strtotime(array_key_first($daily)))) ?></span><span><?= escape(date('d.m', strtotime(array_key_last($daily)))) ?></span></div>
    <?php
}

$home = dirname(__DIR__, 3);
$authPath = $home . '/.astraterm-analytics/auth.json';
$auth = json_decode(@file_get_contents($authPath, false, null, 0, 4096) ?: '{}', true);
$configured = is_array($auth)
    && is_string($auth['salt'] ?? null) && preg_match('/\A[a-f0-9]{32}\z/', $auth['salt'])
    && is_string($auth['hash'] ?? null) && preg_match('/\A[a-f0-9]{64}\z/', $auth['hash']);
$cookie = ['path' => '/admin/', 'secure' => true, 'httponly' => true, 'samesite' => 'Strict'];
$loggedIn = false;
$error = '';
$stats = null;
$token = '';
$period = $_GET['period'] ?? '7';
$periods = ['1' => 'Сегодня', '7' => '7 дней', '30' => '30 дней', 'all' => 'Вся история'];
if (!is_string($period) || !isset($periods[$period])) {
    $period = '7';
}

if ($configured) {
    session_name('ASTRATERM_ADMIN');
    session_set_cookie_params($cookie);
    session_cache_limiter('');
    $configured = session_start(['use_strict_mode' => 1, 'use_only_cookies' => 1]);
}
if (!$configured) {
    http_response_code(503);
    $error = 'Вход временно недоступен.';
} else {
    $token = $_SESSION['csrf'] ??= bin2hex(random_bytes(32));
    $loggedIn = ($_SESSION['until'] ?? 0) > time() && ($_SESSION['account'] ?? '') === $auth['hash'];
    if ($_SERVER['REQUEST_METHOD'] === 'POST') {
        $formToken = $_POST['csrf'] ?? null;
        if (!is_string($formToken) || !hash_equals($token, $formToken)) {
            http_response_code(403);
            $error = 'Форма устарела. Обновите страницу.';
        } elseif ($loggedIn && ($_POST['action'] ?? null) === 'logout') {
            $_SESSION = [];
            session_destroy();
            setcookie(session_name(), '', ['expires' => 1] + $cookie);
            header('Location: /admin/', true, 303);
            exit;
        } elseif (!$loggedIn && ($_POST['action'] ?? null) === 'login') {
            $password = $_POST['password'] ?? null;
            if (($_SESSION['retry_after'] ?? 0) > time()) {
                http_response_code(429);
                $error = 'Подождите несколько секунд и повторите вход.';
            } elseif (is_string($password) && strlen($password) <= 1024
                && hash_equals($auth['hash'], hash_pbkdf2('sha256', $password, hex2bin($auth['salt']), 600000, 64))
                && session_regenerate_id(true)) {
                $_SESSION = ['csrf' => bin2hex(random_bytes(32)), 'until' => time() + 28800, 'account' => $auth['hash']];
                header('Location: /admin/', true, 303);
                exit;
            } else {
                $_SESSION['retry_after'] = time() + 3;
                http_response_code(403);
                $error = 'Пароль не подошёл.';
            }
        }
    }
    session_write_close();
    if ($loggedIn) {
        $stats = dashboardStatistics($home . '/logs/astraterminal.ru.access.log', dirname(__DIR__) . '/releases',
            $period, new DateTimeImmutable('now', new DateTimeZone('Europe/Moscow')));
        if (!$stats['readable']) {
            http_response_code(503);
        }
    }
}
?>
<!doctype html>
<html lang="ru">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="noindex, nofollow">
    <title>Статистика сайта · AstraTerm</title>
    <link rel="stylesheet" href="/admin/dashboard.css">
</head>
<body>
<header class="topbar">
    <div class="shell topbar-inner">
        <a class="brand" href="/" aria-label="AstraTerm, главная страница"><span aria-hidden="true">›_</span> AstraTerm</a>
        <?php if ($loggedIn): ?>
            <form method="post" action="/admin/">
                <input type="hidden" name="csrf" value="<?= escape($token) ?>">
                <button name="action" value="logout" class="secondary">Выйти</button>
            </form>
        <?php endif; ?>
    </div>
</header>
<?php if (!$loggedIn): ?>
<main class="login shell">
    <section class="panel login-card">
        <div class="eyebrow">АДМИН-ПАНЕЛЬ</div>
        <h1>Вход в статистику</h1>
        <p class="muted">Используйте отдельный пароль администратора сайта.</p>
        <?php if ($error !== ''): ?><p class="notice" role="alert"><?= escape($error) ?></p><?php endif; ?>
        <?php if ($configured): ?>
            <form method="post" action="/admin/" class="login-form">
                <input type="hidden" name="csrf" value="<?= escape($token) ?>">
                <label for="password">Пароль</label>
                <input id="password" name="password" type="password" autocomplete="current-password" required maxlength="1024">
                <button name="action" value="login">Войти</button>
            </form>
        <?php endif; ?>
    </section>
</main>
<?php else: ?>
<main class="shell dashboard">
    <div class="heading">
        <div><div class="eyebrow">ASTRATERMINAL.RU</div><h1>Статистика сайта</h1></div>
        <nav class="periods" aria-label="Период статистики">
            <?php foreach ($periods as $value => $label): ?>
                <a href="?period=<?= escape((string) $value) ?>"<?= (string) $value === $period ? ' aria-current="page"' : '' ?>><?= escape($label) ?></a>
            <?php endforeach; ?>
        </nav>
    </div>
    <?php if (!$stats['readable']): ?>
        <p class="notice" role="status">Журнал запросов недоступен. Обновите страницу позже.</p>
    <?php else: ?>
        <p class="coverage muted">Журналы <?= $stats['firstDay'] === null ? 'пока не содержат записей' : 'за ' . escape($stats['firstDay']) . ' — ' . escape($stats['lastDay']) ?> · Московское время</p>
        <?php if ($stats['incomplete']): ?><p class="notice" role="status">Часть журнала недоступна. Итоги рассчитаны по прочитанным записям.</p><?php endif; ?>
        <section class="metrics" aria-label="Итоги периода">
            <div class="panel metric"><span>Визиты</span><strong><?= number($stats['visits']) ?></strong><small>Оценка по сессиям</small></div>
            <div class="panel metric"><span>Просмотры страниц</span><strong><?= number($stats['pageViews']) ?></strong><small>Главная и варианты сайта</small></div>
            <div class="panel metric"><span>Полные скачивания</span><strong><?= number($stats['full']) ?></strong><small>Передан весь файл</small></div>
            <div class="panel metric"><span>Частичные запросы</span><strong><?= number($stats['partial']) ?></strong><small>Ответы HTTP 206</small></div>
        </section>
        <section class="charts" aria-label="Динамика по дням">
            <article class="panel chart-card"><h2>Визиты по дням</h2><?php chart($stats['daily'], 'visits', 'Визиты'); ?></article>
            <article class="panel chart-card"><h2>Скачивания по дням</h2><?php chart($stats['daily'], 'downloads', 'Полные скачивания'); ?></article>
        </section>
        <?php if ($stats['daily']): ?>
        <details class="panel daily-data">
            <summary>Данные по дням</summary>
            <div class="table-scroll"><table><thead><tr><th scope="col">День</th><th scope="col">Визиты</th><th scope="col">Просмотры</th><th scope="col">Скачивания</th></tr></thead>
            <tbody><?php foreach (array_reverse($stats['daily'], true) as $day => $values): ?>
                <tr><th scope="row"><?= escape($day) ?></th><td><?= number($values['visits']) ?></td><td><?= number($values['views']) ?></td><td><?= number($values['downloads']) ?></td></tr>
            <?php endforeach; ?></tbody></table></div>
        </details>
        <?php endif; ?>
        <section class="panel files">
            <div class="section-title"><h2>Файлы программы</h2><span class="muted"><?= number(count($stats['files'])) ?> в разделе релизов</span></div>
            <?php if (!$stats['files']): ?><p class="muted empty">Опубликованных пакетов пока нет.</p><?php else: ?>
            <div class="table-scroll"><table>
                <thead><tr><th scope="col">Файл</th><th scope="col">Размер</th><th scope="col">Скачано</th><th scope="col">Части 206</th><th scope="col">Неполные 200</th><th scope="col">Передано</th></tr></thead>
                <tbody>
                <?php foreach ($stats['files'] as $file): ?>
                    <tr><th scope="row"><span class="filename"><?= escape($file['name']) ?></span><small><?= escape($file['version']) ?></small></th>
                        <td><?= bytesLabel($file['size']) ?></td><td class="total"><?= number($file['full']) ?></td><td><?= number($file['partial']) ?></td><td><?= number($file['interrupted']) ?></td><td><?= bytesLabel($file['bytes']) ?></td></tr>
                <?php endforeach; ?>
                </tbody>
            </table></div>
            <?php endif; ?>
        </section>
        <p class="method muted">Визит начинается после 30 минут без просмотров с того же адреса и браузера. Известные роботы исключены из просмотров. Полное скачивание учитывается, когда ответ 200 передал весь файл. Запросы обновлятора учитываются вместе с прямыми скачиваниями.</p>
    <?php endif; ?>
</main>
<?php endif; ?>
</body>
</html>
