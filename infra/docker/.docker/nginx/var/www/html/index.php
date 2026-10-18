<?php
echo "<h1>Host A - PHP Working Correctly</h1>";
echo "<p>Date: " . date("Y-m-d H:i:s") . "</p>";
echo "<p>Client IP: " . $_SERVER["REMOTE_ADDR"] . "</p>";
echo "<p>Host: " . $_SERVER["HTTP_HOST"] . "</p>";
phpinfo();
?>
