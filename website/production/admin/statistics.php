<?php
declare(strict_types=1);

function publishedFiles(string $releaseRoot): array
{
    $files = [];
    foreach (glob($releaseRoot . '/*/*', GLOB_NOSORT) ?: [] as $filename) {
        $version = basename(dirname($filename));
        $name = basename($filename);
        if (!preg_match('/\A[0-9]+\.[0-9]+\.[0-9]+\z/', $version)
            || !preg_match('/\A[A-Za-z0-9._-]+\.(?:exe|msi|deb|rpm|zip|tar\.gz|AppImage)\z/i', $name)
            || is_link($filename) || is_link(dirname($filename)) || !is_file($filename)) {
            continue;
        }
        $size = filesize($filename);
        if ($size === false || $size <= 0) {
            continue;
        }
        $files['/releases/' . $version . '/' . $name] = [
            'name' => $name, 'version' => $version, 'size' => $size,
            'full' => 0, 'partial' => 0, 'interrupted' => 0, 'bytes' => 0,
        ];
    }
    uasort($files, static function (array $a, array $b): int {
        return version_compare($b['version'], $a['version']) ?: strcmp($a['name'], $b['name']);
    });
    return $files;
}

function accessEvent(string $line): ?array
{
    $pattern = '~\A(\S+) \S+ \S+ \[([^\]]+)\] "GET ([^" ]+) HTTP/[0-9.]+" (200|206|304) (\d+|-)(?: "[^"]*" "([^"]*)")?~';
    if (!preg_match($pattern, $line, $match)) {
        return null;
    }
    $time = DateTimeImmutable::createFromFormat('!d/M/Y:H:i:s O', $match[2]);
    if ($time === false) {
        return null;
    }
    return [
        'address' => $match[1], 'time' => $time,
        'path' => rawurldecode(explode('?', $match[3], 2)[0]),
        'status' => (int) $match[4], 'bytes' => (int) $match[5],
        'agent' => $match[6] ?? '',
    ];
}

function dashboardStatistics(string $logPath, string $releaseRoot, string $period, DateTimeImmutable $now): array
{
    $files = publishedFiles($releaseRoot);
    $pages = ['/', '/index.html', '/alternatives', '/alternatives/', '/alternatives/index.html'];
    foreach (['graphite', 'paper', 'signal'] as $variant) {
        $path = '/alternatives/' . $variant;
        array_push($pages, $path, $path . '/', $path . '/index.html');
    }
    $today = $now->setTime(0, 0);
    $start = $period === 'all' ? null : $today->modify('-' . ((int) $period - 1) . ' days');
    $startTime = $start?->getTimestamp() ?? 0;
    $journals = [$logPath];
    for ($rotation = 1; $rotation <= 10; ++$rotation) {
        $journals[] = $logPath . '.' . $rotation . '.gz';
    }
    $timezone = $now->getTimezone();
    $visitorKey = random_bytes(32);
    $visitors = [];
    $daily = [];
    $firstDay = null;
    $lastDay = null;
    $pageViews = 0;
    $visits = 0;
    $full = 0;
    $partial = 0;
    $readable = 0;
    $incomplete = false;
    $budget = 16 * 1024 * 1024;
    $emptyDay = ['views' => 0, 'visits' => 0, 'downloads' => 0];

    foreach ($journals as $journal) {
        if (!is_file($journal)) {
            continue;
        }
        $stream = @gzopen($journal, 'rb');
        if ($stream === false) {
            $incomplete = true;
            continue;
        }
        ++$readable;
        $continuation = false;
        while (($line = gzgets($stream, 16385)) !== false) {
            $budget -= strlen($line);
            if ($budget < 0) {
                $incomplete = true;
                break;
            }
            $complete = str_ends_with($line, "\n");
            $event = $complete && !$continuation ? accessEvent($line) : null;
            $continuation = !$complete;
            if ($event === null || $event['time'] > $now) {
                continue;
            }
            $date = $event['time']->setTimezone($timezone);
            $day = $date->format('Y-m-d');
            $firstDay = $firstDay === null ? $day : min($firstDay, $day);
            $lastDay = $lastDay === null ? $day : max($lastDay, $day);
            $timestamp = $date->getTimestamp();
            if ($timestamp < $startTime - 1800) {
                continue;
            }

            $path = $event['path'];
            $inPeriod = $timestamp >= $startTime;
            if ($inPeriod && isset($files[$path]) && $event['status'] !== 304) {
                $file = &$files[$path];
                $file['bytes'] += $event['bytes'];
                if ($event['status'] === 206) {
                    ++$file['partial'];
                    ++$partial;
                } elseif ($event['bytes'] === $file['size']) {
                    ++$file['full'];
                    ++$full;
                    $daily[$day] ??= $emptyDay;
                    ++$daily[$day]['downloads'];
                } else {
                    ++$file['interrupted'];
                }
                unset($file);
            }
            if ($event['status'] === 206 || !in_array($path, $pages, true)
                || $event['agent'] === '' || $event['agent'] === '-'
                || preg_match('/bot|spider|crawler|headless|preview|^curl|^wget|^python|^go-http-client/i', $event['agent'])) {
                continue;
            }
            if ($inPeriod) {
                ++$pageViews;
                $daily[$day] ??= $emptyDay;
                ++$daily[$day]['views'];
            }
            // The random key and visitor identifiers live only during this request.
            $identity = hash_hmac('sha256', $event['address'] . "\0" . $event['agent'], $visitorKey);
            $visitors[$identity][] = $timestamp;
        }
        if (!gzeof($stream)) {
            $incomplete = true;
        }
        gzclose($stream);
        if ($budget < 0) {
            break;
        }
    }

    foreach ($visitors as $times) {
        sort($times, SORT_NUMERIC);
        $previous = null;
        foreach ($times as $time) {
            if ($time >= $startTime && ($previous === null || $time - $previous >= 1800)) {
                ++$visits;
                $day = (new DateTimeImmutable('@' . $time))->setTimezone($timezone)->format('Y-m-d');
                ++$daily[$day]['visits'];
            }
            $previous = $time;
        }
    }
    if ($firstDay !== null) {
        $graphStart = max($firstDay, $today->modify('-30 days')->format('Y-m-d'));
        if ($start !== null) {
            $graphStart = max($graphStart, $start->format('Y-m-d'));
        }
        for ($date = new DateTimeImmutable($graphStart, $timezone); $date <= $today; $date = $date->modify('+1 day')) {
            $daily[$date->format('Y-m-d')] ??= $emptyDay;
        }
    }
    ksort($daily);
    return [
        'files' => $files, 'daily' => array_slice($daily, -31, null, true),
        'firstDay' => $firstDay, 'lastDay' => $lastDay, 'readable' => $readable, 'incomplete' => $incomplete,
        'pageViews' => $pageViews, 'visits' => $visits, 'full' => $full, 'partial' => $partial,
    ];
}
