<?php
declare(strict_types=1);

require __DIR__ . '/../website/production/admin/statistics.php';

function check(bool $condition, string $description): void
{
    if (!$condition) {
        throw new RuntimeException($description);
    }
}

$root = sys_get_temp_dir() . '/astraterm-stats-' . bin2hex(random_bytes(8));
$release = $root . '/releases/0.10.0';
$journal = $root . '/access.log';
$windows = '/releases/0.10.0/AstraTerm.exe';
$linux = '/releases/0.10.0/AstraTerm.deb';
mkdir($release, 0700, true);
file_put_contents($release . '/AstraTerm.exe', str_repeat('x', 100));
file_put_contents($release . '/AstraTerm.deb', str_repeat('x', 20));
$browser = 'Mozilla/5.0 fixture';
$log = static function (string $time, string $request, int $status, int $bytes, string $agent) {
    return '192.0.2.10 - - [' . $time . ' +0300] "' . $request . ' HTTP/1.1" ' . $status . ' ' . $bytes . ' "-" "' . $agent . '"' . "\n";
};
try {
    file_put_contents($journal . '.1.gz', gzencode(
        $log('09/Nov/2026:23:57:00', 'GET /', 200, 123, $browser)
        . $log('09/Nov/2026:23:58:00', 'GET ' . $windows, 200, 100, 'AstraTerm/0.10.0')
    ));
    file_put_contents($journal,
        $log('10/Nov/2026:00:01:00', 'GET /alternatives/paper/?ref=home', 304, 0, $browser)
        . $log('10/Nov/2026:00:32:00', 'GET /', 200, 123, $browser)
        . $log('10/Nov/2026:00:40:00', 'GET /', 200, 123, 'ExampleCrawler')
        . $log('10/Nov/2026:00:50:00', 'GET /admin/', 200, 123, $browser)
        . $log('10/Nov/2026:00:51:00', 'HEAD ' . $windows, 200, 100, $browser)
        . $log('10/Nov/2026:00:52:00', 'GET ' . $windows, 200, 90, $browser)
        . $log('10/Nov/2026:00:53:00', 'GET ' . $windows, 206, 10, $browser)
        . $log('10/Nov/2026:00:54:00', 'GET ' . $windows, 206, 90, $browser)
        . $log('10/Nov/2026:00:55:00', 'GET ' . $linux, 404, 20, $browser)
    );
    $now = new DateTimeImmutable('2026-11-10 12:00', new DateTimeZone('Europe/Moscow'));
    $stats = dashboardStatistics($journal, $root . '/releases', 'all', $now);
    check($stats['readable'] === 2 && !$stats['incomplete'], 'Read active and compressed logs');
    check($stats['visits'] === 2 && $stats['pageViews'] === 3, 'Group visits across rotation, exclude bots and admin');
    check($stats['full'] === 1 && $stats['partial'] === 2, 'Count complete transfers separately from HTTP ranges');
    check($stats['files'][$windows]['interrupted'] === 1 && $stats['files'][$windows]['bytes'] === 290, 'Count short HTTP 200 and transferred bytes');
    check($stats['files'][$linux]['full'] === 0, 'Show zero downloads for a published file');
    check(!str_contains(json_encode($stats), '192.0.2.10'), 'Visitor addresses stay out of aggregate');
    $today = dashboardStatistics($journal, $root . '/releases', '1', $now);
    check($today['full'] === 0 && $today['pageViews'] === 2 && $today['visits'] === 1, 'Filter by Moscow calendar without restarting a visit at midnight');
    check(dashboardStatistics($root . '/absent.log', $root . '/releases', 'all', $now)['readable'] === 0, 'Report an unavailable log');
    echo "Website statistics checks passed.\n";
} finally {
    unlink($journal);
    unlink($journal . '.1.gz');
    unlink($release . '/AstraTerm.exe');
    unlink($release . '/AstraTerm.deb');
    rmdir($release);
    rmdir($root . '/releases');
    rmdir($root);
}
