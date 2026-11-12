<?php
// Insecure eval demo — DO NOT use anywhere else
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_POST['code'])) {
    $code = $_POST['code']; // intentionally not sanitized
    echo "<pre>Result:\n";
    eval($code); // demo only
    echo "</pre>";
    exit;
}
?>
<!doctype html>
<html>
  <body>
    <h2>Insecure Eval (lab)</h2>
    <form method="post">
      <textarea name="code" rows="6" cols="60">echo php_uname();</textarea><br/>
      <button type="submit">Run</button>
    </form>
  </body>
</html>

