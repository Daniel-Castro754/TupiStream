# Política de Segurança

A segurança é especialmente importante no Tupi Stream porque integra serviços que podem utilizar tokens pessoais de usuários.

## Reportando uma vulnerabilidade

Se você encontrar uma falha que possa expor tokens, credenciais, sessões, dados pessoais ou permitir acesso não autorizado, evite publicar detalhes exploráveis em uma issue pública.

Entre em contato com o mantenedor por um canal privado disponível no perfil do GitHub e inclua, quando possível:

- descrição da vulnerabilidade;
- componente afetado;
- passos mínimos para reprodução;
- impacto esperado;
- sugestão de correção, se houver.

## Limitação conhecida

A implementação atual pode incluir o token Real-Debrid no caminho da URL do manifest e mantê-lo temporariamente em sessões armazenadas no cache SQLite.

Por isso:

- não compartilhe sua URL pessoal de manifest;
- evite publicar logs contendo URLs completas;
- trate `data/cache.db` como dado sensível;
- gere um novo token se houver suspeita de exposição.

A remoção dessa exposição é considerada uma prioridade técnica.

## Boas práticas para contribuidores

Nunca envie para o repositório:

- tokens Real-Debrid;
- chaves TMDB;
- arquivos `.env` reais;
- cookies de sessão;
- dumps de banco contendo credenciais;
- logs com URLs privadas completas.
