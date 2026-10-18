<?php
$conn = new mysqli("localhost","attacker","Attack3rPass","sensitive_data");
if($conn->connect_error){
    die("Connection failed: ".$conn->connect_error);
}
$result = $conn->query("SELECT * FROM credentials");
echo "<h1>Stolen Credentials</h1>";
echo "<table border='1'><tr><th>ID</th><th>Username</th><th>Password Hash</th></tr>";
while($row = $result->fetch_assoc()) {
    echo "<tr><td>".$row['id']."</td><td>".$row['username']."</td><td>".$row['password']."</td></tr>";
}
echo "</table>";
$conn->close();
?>
