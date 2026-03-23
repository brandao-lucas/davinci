## 13. Checklist Pré-Desenvolvimento

Antes de escrever a primeira linha de código, confirme que:

- [ ] PostgreSQL 16+ está instalado e rodando
- [ ] Redis está instalado e rodando
- [ ] Rust toolchain está instalado (`rustup show`)
- [ ] Maturin está instalado (`pip install maturin`)
- [ ] NCBI API key foi obtida em https://www.ncbi.nlm.nih.gov/account/settings/
- [ ] Django project foi criado com a estrutura da Seção 4
- [ ] `docker-compose up -d` levanta Postgres + Redis
- [ ] `python manage.py migrate` roda sem erros
- [ ] Triggers FTS foram aplicados (migration RunSQL ou script manual)
- [ ] `maturin develop --release` compila o Rust engine sem erros
- [ ] `import rust_engine` funciona no Python shell

---