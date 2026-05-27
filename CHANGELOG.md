# Changelog

## [Unreleased] — 2026-05-27

### `scripts/crawl_api.py` — streaming em tempo real para Tinybird

Substituídas as gravações locais (`merchants.jsonl`, `points.jsonl`, `crawl.log`) por
envio em tempo real de eventos HTTP para o datasource `ifood_events` no Tinybird.

**O que mudou:**

- Removidas funções `_save_meta` e `_load_resume_state` (escrita local)
- Removidas abertura de arquivos JSONL no loop principal
- Adicionadas funções `_tb_send`, `_tb_api_request` para envio ao Tinybird via NDJSON
- `fetch_point()` agora aceita parâmetro `tb` — envia evento `api_request` após cada
  request ao iFood, independente do status (sucesso ou erro)
- `fetch_all_pages()` propaga `tb` para `fetch_point` e também envia evento em cada
  request de página adicional (cursor `NEXT_CONTENT`)
- Formato do evento segue o schema `ifood_events`:
  `event_type`, `device_id`, `event_data` (JSON string com campos completos de
  request e response)
- `--resume` mantido: lê `points.jsonl` e `merchants.jsonl` de capturas locais
  anteriores para retomar de onde parou, mas novos dados vão apenas ao Tinybird

---

## [Unreleased] — 2026-05-25

### Problema corrigido: 73 → ~400 restaurantes por ponto de grade

O crawler capturava apenas ~73 dos ~400 restaurantes visíveis no iFood.
Três bloqueios independentes foram identificados e corrigidos:

---

### `scripts/crawl_api.py` — novo arquivo

Crawler API-direto com paginação completa. Browser sobe **uma única vez** para
capturar a sessão; todos os requests `bm/home` são feitos via httpx.

**Causa 1 corrigida — Paginação não implementada:**
- `fetch_all_pages()`: percorre todas as páginas de um ponto seguindo o cursor
  `NEXT_CONTENT` retornado pela API até não haver mais resultados
- `_extract_pagination()`: extrai `(cursor, section_id, alias)` do card de tipo
  `NEXT_CONTENT`, que fica num card separado dentro da mesma section dos merchants
- `Session.build_paginated_url()`: monta URLs de paginação com `section`, `cursor`
  e `alias` corretos

**Causa 2 corrigida — Token de paginação em lugar inesperado:**
- O cursor não está dentro do card `MERCHANT_LIST_V2` como seria convencional —
  fica num card separado de tipo `NEXT_CONTENT`, com action
  `card-content?cursor=<TOKEN>`. Identificado via inspeção do JSON real da API.

**Causa 3 corrigida — Header de segurança ausente:**
- HUMAN Security (ex-PerimeterX) exige que os cookies de sessão `_px3`, `_pxhd`,
  `_pxvid` sejam enviados também no header `x-px-cookies` além do `Cookie` padrão.
  Sem esse header a API retorna HTTP 403 mesmo com `aAccessToken` válido.
- `capture_session()` extrai todos os cookies PX via CDP e constrói o header
  `x-px-cookies` com a ordem canônica `_px3 → _pxhd → _pxvid → pxcts`.
- `Session.cookies` agora inclui o cookie jar completo repassado no httpx.

**Outros:**
- Renovação automática de sessão após TTL de 45 min ou 3 erros 401/403
  consecutivos
- STEALTH_SCRIPT com patches específicos do sensor HUMAN Security / PerimeterX:
  `Element.prototype.getAttribute`, `domAutomation*`, `window.external`,
  `Function.prototype.toString` via WeakSet

---

### `scripts/crawl.py` — modificado

- Adicionados patches do HUMAN Security / PerimeterX ao `STEALTH_SCRIPT`:
  - `Element.prototype.getAttribute` → retorna `null` para `'webdriver'`
    (CDP seta o atributo no `documentElement`; PX checa separado do `navigator`)
  - `domAutomation`, `domAutomationController`, `_Selenium_IDE_Recorder`,
    `__webdriver_script_fn` → `undefined`
  - `window.external` mock com `AddSearchProvider` / `IsSearchProviderInstalled`
  - `Function.prototype.toString` proxy via WeakSet para que funções patchadas
    exponham `[native code]`
- `page_size` padrão alterado de 50 → 100 (tanto em `crawl_point()` quanto no
  argumento CLI `--page-size`)

---

### `scripts/crawl_catalog.py` — reescrito

Migrado de abordagem browser-por-loja para API direta:

- Browser sobe **uma vez** para capturar sessão (igual ao `crawl_api.py`)
- Catálogos buscados via `httpx.AsyncClient` com headers de auth + cookies PX
- `access_key` / `secret_key` interceptados via CDP enquanto o browser navega
  para a primeira loja; fallback hardcoded se timeout
- `CatalogSession` inclui cookies PX capturados via CDP
- Pastas de saída renomeadas para `{nome}-{uuid}` (nome antes do UUID)
- Renovação de sessão automática após TTL ou 3 erros consecutivos

---

### `configs/cities.py` — modificado

- Adicionada cidade **São João del Rei - MG**: bounding box calibrado pelo log de
  execução de 2026-05-22 (`step_km=2.0`, ~36 pontos de grade)

---

### `JOURNAL.md` — atualizado

- **Tentativa 16**: HUMAN Security (PerimeterX) bypass — documentação completa dos
  três vetores corrigidos, resultado do teste (105 merchants em 3 pontos) e
  diagnóstico de cookies PX
- Lições aprendidas 15–18: HUMAN Security ≠ Akamai, cookie `_px3` obrigatório em
  httpx, patch `Element.prototype.getAttribute` para CDP, `Function.prototype.toString`
  checado pelo sensor PX

---

## Histórico anterior

| Commit    | Descrição |
|-----------|-----------|
| `4713303` | crawl_catalog: nome da loja antes do uuid, remove products.jsonl |
| `1e45f4b` | Rewrite crawl_catalog.py: per-store folders inside capture dir |
| `9c9cb48` | Add boundary filtering, map dashboard, session check, and Frida hook |
| `4336af2` | Remove obsolete research scripts |
| `8dbb114` | Add multi-city support and rename main crawler to crawl.py |
| `b4df73d` | Add catalog crawler and extend merchant fields |
| `1c549b2` | Extract merchant names and links, isolate Chrome by PID, clean up README |
| `1d6f3b0` | Add development journal with all crawler attempts and lessons learned |
| `01c3f6a` | Switch API capture from JS injection to CDP network events |
| `22ba94f` | Add nodriver-based crawler (crawl_sp_web_nd.py) |
