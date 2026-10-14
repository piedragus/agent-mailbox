# Contribuir / Contributing

Issues y PRs bienvenidos (español o English).

- Antes de un PR grande, abrí un issue contando la idea.
- Tests: `cd broker && pytest` y `cd daemon && pytest` deben pasar. Los cambios de comportamiento llevan test.
- Mantené el broker simple: Flask + SQLite, sin dependencias de servicios externos.
- Seguridad: no abras un issue público por una vulnerabilidad; ver [SECURITY.md](SECURITY.md).
