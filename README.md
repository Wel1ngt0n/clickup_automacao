# Webhook de dependências do ClickUp

Este projeto pode rodar continuamente no Render. Sempre que uma tarefa do
ClickUp for criada ou atualizada, o webhook lê seu campo `_father_task_id` e
garante que ela esteja cadastrada como dependência do card principal. O serviço
também recalcula o status do card principal pela primeira etapa ainda pendente.

A operação é idempotente: se a dependência já existir, nenhuma alteração é
feita.

## Publicação no Render

1. Suba estes arquivos em um repositório do GitHub.
2. No Render, selecione **New > Blueprint** e conecte o repositório.
3. O Render lerá o arquivo `render.yaml`.
4. Preencha `CLICKUP_API_TOKEN` com seu token pessoal do ClickUp.
5. Inicialmente, preencha `CLICKUP_WEBHOOK_SECRET` com um valor temporário.
6. Aguarde o deploy e confirme que `https://SEU-SERVICO.onrender.com/health`
   retorna `{"status":"ok"}`.

O serviço usa Flask com Gunicorn e recebe eventos em:

```text
https://SEU-SERVICO.onrender.com/webhook/clickup
```

## Registrar o webhook no ClickUp

Com o serviço já publicado, execute localmente:

```powershell
$env:CLICKUP_API_TOKEN = "seu_token"
python .\register_webhook.py --url "https://SEU-SERVICO.onrender.com/webhook/clickup"
```

O ClickUp retornará um campo `secret`. Copie exatamente esse valor para a
variável `CLICKUP_WEBHOOK_SECRET` no Render e faça um novo deploy.

O segredo é usado para validar o cabeçalho `X-Signature`. Requisições sem uma
assinatura válida são recusadas.

## Funcionamento

O webhook foca na etapa de **integração da loja** (etapa 5 / `Integração ERP`):

1. **Dependência Automática**: Garante que o card de integração seja adicionado como dependência do card principal da loja (`parent is waiting on integration step`).
2. **Reconciliação de Status**: Quando as etapas iniciais (Cadastro Omie, Onboarding e Criar Loja) estiverem todas concluídas e o card de integração estiver pendente, o webhook verifica o status do card principal. Caso esteja em um status divergente (diferente de `integração erp`), ele corrige o status do card principal para `integração erp`.
3. **Não interfere nas outras etapas**: Não cria dependências desnecessárias para as etapas iniciais nem altera status quando o card ainda está nas etapas iniciais ou após a integração já estar concluída, preservando as automações nativas do quadro.

### Fluxo de execução:

1. Ao receber um evento de tarefa no webhook, busca os dados da tarefa pelo ID.
2. Se for uma etapa (possui `_father_task_id`):
   - Se pertencer a etapas não relacionadas ao fluxo inicial/integração (etapas posteriores como 6, 7, etc.), é ignorado imediatamente.
   - Se for o card de integração ou uma das etapas iniciais (2, 3, 4), localiza o card principal e analisa a loja.
3. Garante a dependência do card de integração no card principal caso ainda não exista.
4. Se as etapas 2, 3 e 4 estiverem concluídas e a integração estiver pendente, corrige o status do card principal para `integração erp` caso esteja divergente.

O serviço conta com:
- **Retry com Backoff Exponencial**: respeita limites de taxa da API do ClickUp (`HTTP 429`) e aguarda o tempo informado no `Retry-After` ou `X-RateLimit-Reset`.
- **Debounce e Lock por Loja**: evita rajadas e chamadas duplicadas simultâneas quando múltiplos eventos da mesma loja chegam juntos.

## Escopo de segurança

O webhook precisa receber eventos do workspace porque as etapas ficam distribuídas
em listas e Espaços diferentes. Isso não significa que ele alterará qualquer card:
antes de criar uma dependência ou mudar um status, o serviço confirma que o card
principal pertence à lista `Implantação de lojas` (`211186088`).

Eventos de outros fluxos são respondidos como `ignored`. Se o webhook fosse
registrado apenas nessa lista, eventos de criação ou conclusão das etapas externas
não chegariam ao serviço e a atualização automática deixaria de funcionar.

Se alguém remover manualmente uma dependência, o próximo evento recebido para a
etapa ou para o card principal faz uma nova auditoria de todas as etapas e recria a
relação ausente. O serviço não recria tarefas apagadas e não restaura um
`_father_task_id` removido, pois nesses casos não existe informação suficiente para
identificar a relação com segurança.

## Auditoria manual

O script localiza as tarefas de etapa pelo campo personalizado `_father_task_id` e
confere se o card principal está aguardando cada uma delas. Por segurança, ele
somente gera o relatório, a menos que `--apply` seja informado.

## Configuração

Crie um token pessoal no ClickUp e defina-o apenas na sessão atual do PowerShell:

```powershell
$env:CLICKUP_API_TOKEN = "seu_token"
```

Não salve o token neste projeto.

## Analisar uma loja

```powershell
python .\clickup_dependency_audit.py --custom-id F0H-797
```

Para adicionar somente as dependências ausentes:

```powershell
python .\clickup_dependency_audit.py --custom-id F0H-797 --apply
```

## Analisar todas as lojas

```powershell
python .\clickup_dependency_audit.py --all
```

Para corrigir todas:

```powershell
python .\clickup_dependency_audit.py --all --apply
```

O escopo padrão já está configurado para o workspace `36953879`, a lista
`Implantação de lojas` (`211186088`) e o campo `_father_task_id`
(`553cb505-acc0-401e-8760-f73879a3aad7`). Esses valores também podem ser
substituídos pelos argumentos `--workspace-id`, `--list-id` e
`--father-field-id`.

O auditor não cria etapas, não remove dependências e não altera status. Ele apenas
garante a relação `card principal aguarda tarefa de etapa` para tarefas já
existentes.
