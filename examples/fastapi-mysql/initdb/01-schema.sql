-- Bind-mounted into the MySQL container by Compose. This mount is exactly the
-- kind of thing that has no direct Kubernetes equivalent: see the migration
-- warnings for how it is handled.
CREATE TABLE IF NOT EXISTS items (
    id   INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL
);
