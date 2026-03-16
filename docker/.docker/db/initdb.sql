-- =========================================
-- CREATE DATABASES
-- =========================================
CREATE DATABASE IF NOT EXISTS app;

-- =========================================
-- USE MAIN DATABASE
-- =========================================
USE app;

-- =========================================
-- USERS TABLE
-- =========================================
CREATE TABLE users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    email VARCHAR(150) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('admin', 'customer') DEFAULT 'customer',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =========================================
-- PRODUCTS TABLE
-- =========================================
CREATE TABLE products (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    description TEXT,
    price DECIMAL(10,2) NOT NULL,
    stock INT DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- =========================================
-- ORDERS TABLE
-- =========================================
CREATE TABLE orders (
    id INT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    status ENUM('pending','paid','shipped','cancelled') DEFAULT 'pending',
    total DECIMAL(10,2) NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

-- =========================================
-- ORDER ITEMS TABLE
-- =========================================
CREATE TABLE order_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL,
    product_id INT NOT NULL,
    quantity INT NOT NULL,
    price DECIMAL(10,2) NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

-- =========================================
-- PAYMENTS TABLE
-- =========================================
CREATE TABLE payments (
    id INT AUTO_INCREMENT PRIMARY KEY,
    order_id INT NOT NULL,
    payment_method ENUM('credit_card','pix','boleto') NOT NULL,
    amount DECIMAL(10,2) NOT NULL,
    paid_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (order_id) REFERENCES orders(id)
);

-- =========================================
-- INSERT SAMPLE DATA
-- =========================================

-- Users
INSERT INTO users (name, email, password_hash, role) VALUES
('Alice Martins', 'alice@example.com', 'hash123', 'admin'),
('Bruno Silva', 'bruno@example.com', 'hash456', 'customer'),
('Carla Souza', 'carla@example.com', 'hash789', 'customer');

-- Products
INSERT INTO products (name, description, price, stock) VALUES
('Notebook Pro 14', 'Laptop 14 inch, 16GB RAM, 512GB SSD', 5999.90, 10),
('Mouse Gamer RGB', 'High precision gaming mouse', 199.90, 50),
('Teclado Mecânico', 'Mechanical keyboard with blue switches', 349.90, 30),
('Monitor 27 4K', '27-inch 4K UHD Monitor', 1899.00, 15);

-- Orders
INSERT INTO orders (user_id, status, total) VALUES
(2, 'paid', 6199.80),
(3, 'pending', 349.90);

-- Order Items
INSERT INTO order_items (order_id, product_id, quantity, price) VALUES
(1, 1, 1, 5999.90),
(1, 2, 1, 199.90),
(2, 3, 1, 349.90);

-- Payments
INSERT INTO payments (order_id, payment_method, amount) VALUES
(1, 'credit_card', 6199.80);

-- =========================================
-- INDEXES FOR PERFORMANCE
-- =========================================
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_orders_user ON orders(user_id);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_order_items_product ON order_items(product_id);

