# Webhook de dependências do ClickUp

## Organização por função

```text
app.py                         Entrada HTTP compartilhada (app:app)
common/                        Cliente ClickUp e leitura de ambiente
scripts/                       Registro e manutenção do webhook
automations/
  integration_repair/          Correção antiga da integração do quadro principal
    config.py                  IDs exclusivos desse quadro
    service.py                 Dependência e correção de status
    audit.py                   Auditoria manual
  partners/                    Automação do novo quadro de parceiros
    config.py                  Modelos e regras de configuração
    service.py                 Cards, checklists, dependências e status
    events.py                  Enfileiramento dos eventos
    state.py                   Persistência PostgreSQL
    worker.py                  Consumo da fila e recuperação periódica
    templates/                 Descrições e checklists editáveis
tests/
  integration_repair/          Regressão da correção antiga
  partners/                    Testes do novo quadro
  shared/                      Testes do cliente compartilhado
  test_webhook.py              Assinatura e entrada HTTP
```

As regras dos quadros são independentes. O endpoint `/webhook/clickup`
identifica o fluxo e encaminha para o módulo correspondente. Os comandos antigos
`clickup_dependency_audit.py` e `register_webhook.py` continuam válidos como
entradas de compatibilidade; a implementação fica nas pastas acima.


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

## Modelos de cards para parceiros

Os modelos editáveis das nove etapas estão em
[`automations/partners/templates`](automations/partners/templates/README.md). Cada etapa tem uma
descrição própria e um checklist inicial, aprovados para o teste do fluxo.
Os nomes dos cards, responsáveis, prazos e itens de checklist ficam em
[`modelos.json`](automations/partners/templates/modelos.json); as instruções ficam nos
arquivos Markdown dessa pasta.

O fluxo de parceiros é separado da correção de integração do quadro principal.
Cliente novo recebe nove etapas; cliente Instabuy existente recebe oito, sem
Cadastro de produtos. Financeiro fica entre Treinamento e Pós-Implantação.
O serviço cria descrições, checklists nativos (itens desmarcados) e dependências;
o status principal acompanha a primeira etapa obrigatória pendente. Reabrir uma
etapa faz o principal voltar para ela no próximo ciclo.

Onboarding, Criar Loja, Cadastro de produtos, Treinamento e Pós-Implantação herdam
inicialmente o responsável do principal. ERP/PDV vai para Guilherme; Marketplace
e Qualidade para Welington; Financeiro para Vanessa. O serviço aguarda a definição
do implantador quando necessário e preserva reatribuições manuais posteriores.
As regras editáveis estão no guia dos modelos.

Enquanto o quadro estiver privado para teste, o `.env` local limita as
atribuições à conta de Welington por `CLICKUP_PARTNER_ALLOWED_ASSIGNEE_IDS`.
Os responsáveis dos demais usuários ficam configurados, sem tentativas de
compartilhar o quadro ou conceder acesso automaticamente.

## Testar parceiros no Docker local

1. O arquivo `.env` local está preparado. Preencha `CLICKUP_API_TOKEN` no seu
   computador. Não compartilhe a chave nem a adicione ao Git.
2. Suba o banco e valide os modelos:

   ```powershell
   docker compose up -d --wait postgres
   docker compose --profile automation build worker
   docker compose --profile automation run --rm worker python -m automations.partners.worker --validate
   ```

3. Confira o acesso ao ClickUp sem criar tarefas:

   ```powershell
   docker compose --profile automation run --rm worker python -m automations.partners.worker --check
   ```

4. Inicie o processo e acompanhe os logs:

   ```powershell
   docker compose --profile automation up -d worker
   docker compose logs -f worker
   ```

5. Aguarde o log `Fluxo parceiros iniciado`. Só então preencha o
   [formulário de parceiros](https://forms.clickup.com/36953879/f/137qrq-31433/2KAK51GBR9RMYO6ZRM)
   com uma loja de teste. O processo consulta o ClickUp a cada 60 segundos;
   a criação completa pode levar alguns minutos devido ao limite de chamadas.
   Teste primeiro Não e depois outra loja com Sim. Confira as 9/8 tarefas,
   checklists e dependências, conclua etapas e reabra Financeiro para verificar
   o retrocesso de status.

Neste modo, não é necessário expor seu computador ou trocar o webhook do Render.
As consultas são locais, e as alterações são feitas no ClickUp real. O Docker
precisa continuar ligado para acompanhar novas lojas e alterações de status.

O consumidor lê a fila a cada 2 segundos, independentemente da consulta de
recuperação de 60 segundos. Um evento recebido no endpoint `/webhook/clickup`
e gravado no mesmo PostgreSQL pode iniciar o processamento no próximo ciclo da
fila, sem aguardar a consulta periódica. `--queue-seconds` ajusta essa leitura;
`--poll-seconds 0` permite operar apenas pelos eventos. A criação completa ainda
respeita o limite da API e pode levar alguns minutos.

O instante do primeiro início fica salvo no banco: os cards antigos não entram
automaticamente. Para testar um card já criado, defina seu ID interno em
`CLICKUP_PARTNER_ALLOWED_PARENT_IDS` no `.env` e recrie apenas o worker
(`docker compose --profile automation up -d --force-recreate worker`). Uma lista
de IDs preenchida restringe a execução somente àqueles projetos.

Para simular uma loja sem criar cards ou gravar no banco:

```powershell
docker compose --profile automation run --rm worker python -m automations.partners.worker --preview-parent ID_INTERNO
```

O PostgreSQL fica acessível apenas localmente, na porta `55432`, e os dados ficam
no volume `partner_pgdata`. A credencial padrão do Compose é exclusivamente de
desenvolvimento local. Para parar o processamento mantendo os dados:

```powershell
docker compose --profile automation stop worker
```

## Recuperação e limites do fluxo de parceiros

O banco registra a reserva antes de criar cada tarefa, checklist e item. Eventos
repetidos reaproveitam o que já foi criado. Se uma resposta se perder, a próxima
consulta tenta localizar o objeto; sem confirmação, registra
`uncertain_creation` e exige revisão, em vez de arriscar uma duplicação.
Não apague reservas ou o volume para forçar uma nova tentativa: primeiro confira
o que foi criado no ClickUp e faça a recuperação com o responsável técnico.

Cards existentes são adotados sem sobrescrever conteúdo. Etapa apagada após ser
registrada, etapa arquivada, duplicidade ou mudança posterior da resposta de
cliente existente gera pendência para revisão. Um principal arquivado fica fora
do processamento. Uma etapa obrigatória ausente não permite finalizar o projeto.
O sistema não conclui etapas pelo checklist e não copia produtos.

## Migração posterior para Supabase/Render

Após homologar, migre também as tabelas `clickup_partner_jobs` e
`clickup_partner_operations`, preservando reservas, snapshots e o limite de data.
Configure `PARTNER_DATABASE_URL` com conexão PostgreSQL direta (ou pool em modo
sessão), não com pool em modo transação, pois o processamento usa advisory lock
de sessão. Não há provisionamento de Supabase ou infraestrutura paga nesta etapa.

No Render, a configuração atual inicia apenas `app:app`. Para ativar parceiros,
é necessário executar também `python -m automations.partners.worker`, conectar o mesmo banco
no web service e no worker e configurar `CLICKUP_PARTNER_ENABLED=true` e o mesmo
`CLICKUP_PARTNER_START_AFTER`/limite de IDs nos dois processos. Só habilitar a flag
do webhook enfileira eventos, mas não inicia o consumidor. Por padrão a flag é
falsa no app e a correção do quadro principal continua funcionando sem banco.
Pare o worker local antes de transferir a execução para um banco diferente.

## Validação de desenvolvimento

```powershell
.\.venv\Scripts\python -m unittest discover -s tests -v
```

Para incluir os testes reais de PostgreSQL local, defina
`PARTNER_TEST_DATABASE_URL` com a conexão de um banco separado para testes, como
`clickup_partners_test` no mesmo PostgreSQL local; não use o banco acompanhado
pelo worker ativo. Os testes
de API usam simulação; nenhum teste automatizado cria lojas no ClickUp real.
O plano e os critérios restantes estão em
[`automacao-parceiros-plano.md`](automacao-parceiros-plano.md).
