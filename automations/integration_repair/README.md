# Correção de integração do quadro principal

A regra de Integração ERP do From Zero to Hero fica em `service.py`; os identificadores desse quadro ficam em `config.py`. O webhook compartilha apenas a entrada HTTP e o cliente ClickUp com o fluxo de parceiros.

Este módulo mantém o comportamento anterior: garante a dependência da integração e corrige o status para Integração ERP quando as etapas iniciais estão concluídas e a integração continua pendente. Não cria tarefas de parceiros e não depende de PostgreSQL.

Auditoria manual, sem escrita por padrão:

```powershell
python -m automations.integration_repair.audit --custom-id F0H-797
```

A opção `--apply` adiciona dependências ausentes. `clickup_dependency_audit.py` na raiz continua como entrada compatível.
