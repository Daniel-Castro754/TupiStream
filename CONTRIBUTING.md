# Contribuindo com o Tupi Stream

Obrigado pelo interesse em contribuir com o projeto.

## Antes de começar

- verifique se já existe uma issue ou pull request sobre o mesmo tema;
- mantenha mudanças pequenas e focadas quando possível;
- não inclua tokens, cookies, credenciais ou dados pessoais em commits, logs ou exemplos;
- respeite os termos de uso das fontes integradas.

## Ambiente local

```bash
git clone https://github.com/Daniel-Castro754/TupiStream.git
cd TupiStream
python -m venv venv
```

Ative o ambiente virtual e instale as dependências:

```bash
pip install -r requirements.txt
cp .env.example .env
python -m app.main
```

## Novas fontes

Novos scrapers devem:

1. herdar de `BaseScraper`;
2. implementar a interface esperada pelo agregador;
3. possuir timeout e tratamento de falhas;
4. evitar bloquear todo o agregador quando uma fonte estiver indisponível;
5. normalizar os resultados para `TorrentResult`;
6. evitar duplicatas sempre que possível;
7. não registrar tokens ou informações sensíveis em logs.

## Pull requests

Ao abrir um PR, descreva:

- o problema que está sendo resolvido;
- o comportamento anterior;
- o comportamento esperado;
- como a alteração foi testada;
- riscos ou limitações conhecidas.

Mudanças grandes de arquitetura devem ser discutidas em uma issue antes da implementação.

## Segurança

Não abra publicamente detalhes de uma vulnerabilidade que possa expor tokens, credenciais ou usuários. Consulte `SECURITY.md`.
