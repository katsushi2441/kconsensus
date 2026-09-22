<?php
// Kurage 合意点マップ (kconsensus) — kurage.exbridge.jp 上の公開入口。
// 自宅サーバー :18379 への透過プロキシ。UIは相対パス(api/... ../../style.css)なので
// /kconsensus.php/ (末尾スラッシュ) を起点に PATH_INFO で中継する。
// バックエンドURLは同ディレクトリの kconsensus_config.php で定義する(リポジトリには含めない)
//   <?php define('KCONSENSUS_BACKEND', 'http://あなたのサーバー:18379');
//
// **他の製品のプロキシと違う点が2つある。**
//  1) Cookie を双方向に中継する。投票者の識別が cookie の乱数だけなので、
//     中継しないと押すたびに別人になり、意見グループが作れない。
//  2) HTML に <head> がある前提で計測タグを差し込む。kchinjo は templates に
//     </head> が無く、タグが入らないまま公開されていた（2026-09-19 実測）。
$__cfg = __DIR__ . '/kconsensus_config.php';
if (is_file($__cfg)) { require_once $__cfg; }
$BACKEND = defined('KCONSENSUS_BACKEND') ? KCONSENSUS_BACKEND : 'http://127.0.0.1:18379';

// /kconsensus.php → /kconsensus.php/ へ(相対URL解決のため)
if (!isset($_SERVER['PATH_INFO']) || $_SERVER['PATH_INFO'] === '') {
    if (substr($_SERVER['REQUEST_URI'], -1) !== '/' && strpos($_SERVER['REQUEST_URI'], '?') === false) {
        header('Location: /kconsensus.php/', true, 302); exit;
    }
}
$path = isset($_SERVER['PATH_INFO']) ? $_SERVER['PATH_INFO'] : '/';
$qs = isset($_SERVER['QUERY_STRING']) && $_SERVER['QUERY_STRING'] !== '' ? '?' . $_SERVER['QUERY_STRING'] : '';

$ch = curl_init($BACKEND . $path . $qs);
$headers = array('X-Forwarded-Proto: https', 'X-Forwarded-Host: kurage.exbridge.jp');
if (!empty($_SERVER['HTTP_X_FORWARDED_FOR'])) { $headers[] = 'X-Forwarded-For: ' . $_SERVER['HTTP_X_FORWARDED_FOR']; }
elseif (!empty($_SERVER['REMOTE_ADDR'])) { $headers[] = 'X-Forwarded-For: ' . $_SERVER['REMOTE_ADDR']; }
if (!empty($_SERVER['CONTENT_TYPE'])) { $headers[] = 'Content-Type: ' . $_SERVER['CONTENT_TYPE']; }
// 投票者の識別に要る。これを送らないと、押すたびに新しい投票者になる
if (!empty($_SERVER['HTTP_COOKIE'])) { $headers[] = 'Cookie: ' . $_SERVER['HTTP_COOKIE']; }

curl_setopt_array($ch, array(
    CURLOPT_CUSTOMREQUEST => $_SERVER['REQUEST_METHOD'],
    CURLOPT_RETURNTRANSFER => true, CURLOPT_HEADER => true,
    CURLOPT_HTTPHEADER => $headers, CURLOPT_ENCODING => '',
    CURLOPT_TIMEOUT => 60, CURLOPT_FOLLOWLOCATION => false,
));
if ($_SERVER['REQUEST_METHOD'] !== 'GET') {
    curl_setopt($ch, CURLOPT_POSTFIELDS, file_get_contents('php://input'));
}
$res = curl_exec($ch);
if ($res === false) {
    http_response_code(502); header('Content-Type: text/plain; charset=utf-8');
    echo '合意点マップのバックエンドに接続できません'; exit;
}
$status = curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
$hsize = curl_getinfo($ch, CURLINFO_HEADER_SIZE);
curl_close($ch);
http_response_code($status);

$isHtml = false;
foreach (explode("\r\n", substr($res, 0, $hsize)) as $h) {
    if (stripos($h, 'Content-Type:') === 0) {
        header($h);
        if (stripos($h, 'text/html') !== false) { $isHtml = true; }
    } elseif (stripos($h, 'Cache-Control:') === 0 || stripos($h, 'Content-Disposition:') === 0) {
        header($h);
    } elseif (stripos($h, 'Set-Cookie:') === 0) {
        // Path をプロキシ配下に直す。/ のままだとサイト全体に配ってしまう
        $sc = preg_replace('/;\s*Path=[^;]*/i', '; Path=/kconsensus.php/', $h);
        if (stripos($sc, 'Path=') === false) { $sc .= '; Path=/kconsensus.php/'; }
        header($sc, false);   // 第2引数 false = 複数の Set-Cookie を潰さない
    }
}
$body = substr($res, $hsize);
if ($isHtml || strpos($body, '<!doctype html') === 0) {
    $tag = '<script>(function(){var s=document.createElement("script");s.src="https://kurage.exbridge.jp/simpletrack.php?url="+encodeURIComponent(location.href)+"&ref="+encodeURIComponent(document.referrer);s.async=true;document.head.appendChild(s)})();</script>';
    $body = str_replace('</head>', $tag . '</head>', $body);
    // 商品ページへの導線（デモ側だけに出す。アプリ本体は触らない）
    $body = str_replace('</body>', '<p style="text-align:center;font-size:13px;margin:14px 0;color:#5d6b7a"><a href="https://kappstore.exbridge.jp/app.php?id=d88a943736386fb3&amp;ref=kconsensus" target="_blank" rel="noopener">この画面の一式をオンプレミスで導入する（商品ページ）</a></p></body>', $body);
}
echo $body;
