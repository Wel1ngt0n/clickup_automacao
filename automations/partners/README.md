# Automação do quadro de parceiros

- `config.py`: valida modelos e resolve as regras de etapas/responsáveis.
- `service.py`: cria/reaproveita cards, checklists, dependências e atualiza status.
- `events.py`: identifica eventos deste quadro e os coloca na fila.
- `state.py`: fila, snapshots, reservas e locks no PostgreSQL.
- `worker.py`: consome a fila e faz consultas de recuperação.
- [templates/](templates/README.md): descrições e checklists editáveis.

Validar os modelos sem acessar ClickUp ou banco:

```powershell
python -m automations.partners.worker --validate
```

O teste continua no Docker local, com o mesmo banco, volumes e restrição de responsáveis do `.env`. Consulte o [README principal](../../README.md) para iniciar e testar.
