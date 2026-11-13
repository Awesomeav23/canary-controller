-- Separate database for the test suite (`make test-mysql`).
CREATE DATABASE IF NOT EXISTS canary_test;
GRANT ALL PRIVILEGES ON canary_test.* TO 'canary'@'%';
