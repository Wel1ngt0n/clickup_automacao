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

Para cada evento de tarefa:

1. Busca a tarefa pelo ID interno recebido no webhook.
2. Lê o campo `_father_task_id`.
3. Resolve o ID personalizado do pai, como `F0H-797`, para o ID interno.
4. Verifica se o pai já aguarda a etapa.
5. Adiciona somente a dependência ausente.
6. Ordena as etapas pendentes por `flow_step`.
7. Corrige o status do card principal para a primeira etapa pendente.

### Liberação das etapas posteriores

As dependências das etapas a partir do `flow_step` 5 ficam bloqueadas até que as
três etapas iniciais existam e estejam finalizadas:

| `flow_step` | Etapa inicial |
|---:|---|
| 2 | Cadastro Omie |
| 3 | Onboarding |
| 4 | Criar loja |

Enquanto alguma delas estiver ausente ou pendente, tarefas posteriores que já
tenham sido criadas são identificadas, mas suas dependências ficam como
`deferred`. Quando a última etapa inicial for finalizada — normalmente Cadastro
Omie — o evento dispara uma nova análise e todas as dependências posteriores são
configuradas.

Mapeamento aplicado:

| `flow_step` | Status do card principal |
|---:|---|
| 2 | cadastro omie |
| 3 | onboarding |
| 4 | criação de loja |
| 5 | integração erp |
| 6 | cadastro de produtos |
| 7 | subir apps |
| 8 | controle de qualidade |
| 9 | treinamento |
| 10 | loja entregue |

Se alguém avançar manualmente o card principal enquanto uma etapa anterior ainda
estiver pendente, o evento de atualização do próprio card também dispara a
reconciliação e o status volta ao valor correto.

O webhook não cria tarefas nem remove dependências. Ele altera apenas o status do
card principal quando o valor divergir da primeira etapa pendente.

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
