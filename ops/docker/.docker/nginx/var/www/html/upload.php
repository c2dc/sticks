<?php
// Simple insecure upload with login check (for lab use only)
if ($_SERVER['REQUEST_METHOD'] === 'POST' && isset($_FILES['file'])) {
    // Check if user is "logged in" - just verify the POST variable
    $isLoggedIn = isset($_POST['loggedin']) && $_POST['loggedin'] === 'true';
    
    if (!$isLoggedIn) {
        echo "Error: You must be logged in to upload files.";
        exit;
    }
    
    $uploaddir = __DIR__ ;
    if (!is_dir($uploaddir)) mkdir($uploaddir, 0755, true);

    $filename = basename($_FILES['file']['name']);
    $target = $uploaddir . $filename;

    if (move_uploaded_file($_FILES['file']['tmp_name'], $target)) {
        echo "Uploaded as: ".htmlspecialchars($filename);
    } else {
        echo "Upload failed.";
    }
    exit;
}
?>
<!doctype html>
<html>
  <body>
    <h2>Insecure Upload (lab)</h2>
    <form method="post" enctype="multipart/form-data">
      <input type="file" name="file"/>
      <!-- Hidden field for login status -->
      <input type="hidden" name="loggedin" value="true"/>
      <button type="submit">Upload</button>
    </form>
    <p>Uploaded files stored in <code>/var/www/html/</code></p>
    <p><small>Note: loggedin=true is always sent with the form</small></p>
  </body>
</html>
