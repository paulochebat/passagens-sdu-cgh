# Agente de passagens SDU <-> CGH

Coleta preços de ida e volta (quinta -> segunda), alternando a rota a cada semana, e grava tudo em SQLite
para análise de custo x antecedência.

## Rodar

```bash
export SERPAPI_KEY="sua_chave"      # https://serpapi.com (engine google_flights)
python collect.py --plan             # confere as viagens planejadas
python collect.py --provider mock    # testa o pipeline sem gastar créditos
python collect.py                    # coleta real
python collect.py --export csv       # gera passagens_flat.csv
```

## Configuração (config.json)

- `anchor_thursday` + `anchor_route`: uma quinta de referência e a rota daquela semana (`a` = SDU->CGH, `b` = CGH->SDU). As demais semanas alternam a partir dela.
- `start_date`: primeira ida a monitorar (se vazio, usa a próxima quinta). `horizon_weeks`: quantas quintas monitorar a partir dela.
- `collect_mode`: `scheduled` (só a rota da semana) ou `both` (as duas rotas em toda quinta, útil para comparar sentidos).

## Tabelas

- `searches`: uma linha por consulta (data da coleta, rota, datas, dias até o embarque, menor preço, faixa típica do Google).
- `offers`: cada oferta retornada, ordenada por preço.
- `v_daily_min`: menor preço por dia de coleta e viagem.

## Observações

- O preço é o total de ida e volta para o itinerário de ida listado.
- Cada rodada consome 1 consulta por viagem planejada. Com 12 semanas, são 12 por rodada (24 em modo `both`).

## Relatório

`python build_dashboard.py` gera `docs/index.html` (página única, sem dependências externas) com o veredito de compra,
preços por viagem, decisão por viagem e janela de compra por antecedência. O workflow roda esse passo após cada coleta.
