-- Runs once, on first initialisation of the volume.
--
-- A separate database for the integration tests, so that `make test-integration`
-- can drop and recreate tables without touching development data.
CREATE DATABASE eng_remember_test OWNER eng;
