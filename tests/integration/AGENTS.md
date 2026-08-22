# Integration-test scope

Follow `spec/TESTING.md` integration protocol. Run health-check first, export `.env.dev` with
`set -a`, and run unit, spot, and api-wired groups separately. Never truncate pytest output.
