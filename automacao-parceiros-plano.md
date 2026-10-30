# Automação de implantação de parceiros

Plano de 30/10/2026, elaborado com `.agents/agent/orchestrator.md`. Execução autorizada: implementação e teste com PostgreSQL no Docker local; Supabase fica para após a homologação. Código na branch `feat/partner-automation`, sem deploy no Render nesta etapa.

## Regras confirmadas

- Adicionar uma segunda função ao mesmo projeto/webhook no Render; preservar a correção de Integração do From Zero to Hero.
- Atuar exclusivamente no ClickUp. Todos os projetos recebidos pelo formulário são marketplace.
- Cliente Instabuy existente: pular Cadastro de produtos, sem criar tarefa de replicação nem copiar produtos. Cliente novo: incluir Cadastro de produtos.
- Criar cards de etapas, vinculá-los como dependências do principal e atualizar o status pela primeira etapa obrigatória não concluída. Etapas são concluídas pelo time.
- Ordem aprovada abaixo. Proposta operacional: criar as etapas aplicáveis na entrada do projeto, inicialmente como `não iniciado`; o principal indica a primeira pendência.

## Estrutura verificada no ClickUp

Principal: **Implantação Parceiros** (`901327379509`), espaço Operações (`901311390512`). Etapas: pasta **Quadros Parceiros** (`901318457139`).

| Ordem proposta | Lista de etapas | ID da lista | Status do principal |
| --- | --- | --- | --- |
| 1 | Onboarding | `901328073754` | onboarding |
| 2 | Criar Lojas | `901327577128` | criar loja |
| 3 | Integrações ERP/PDV | `901327577146` | integração |
| 4 | Integração (Marketplace) | `901328325681` | integração marketplace |
| 5 | Cadastro de produtos — somente novos clientes | `901327577197` | cadastro produtos |
| 6 | Qualidade | `901327577154` | qualidade |
| 7 | Treinamento | `901327577160` | treinamento |
| 8 | Configuração financeira | `1400600000003634` | configuração financeira |
| 9 | Pós-Implantação(Marketplace) | `901328325698` | pós-implantação |

Todos os requisitos concluídos: `implantado`. Card obrigatório ausente ou arquivado não significa etapa concluída; principal arquivado fica fora da reconciliação.

O formulário `137qrq-31433` já cria o principal nessa lista, exige a resposta de cliente existente e aplica o template `t-86a86qjdy`. Na consulta, havia dois principais sem ID personalizado e nenhuma tarefa nas listas de etapas.

Campos confirmados:

- Cliente existente: `c30d5569-ea0b-4799-b904-80815d513bfb`; Sim: `0144f848-2585-46d1-a966-daceaf49108d` (índice 0); Não: `f93c28ca-f8e1-4814-9d48-6f1d1092ae0d` (índice 1). Resolver pela configuração do campo, incluindo índice zero, sem tratar ausência como Não.
- Vínculo nas etapas: `id_tarefa_pai` (`17f49983-94ed-47a2-9b94-8c83a0d99d79`). Usar ID interno do principal, pois não há custom ID nos exemplos.
- Ordem: `etapa` (`1c2c44a2-b281-4a12-8349-dd115055e6c8`); adotar 1–9 após conferir automações existentes. Nome da loja: `2e3506bb-f020-47f2-9950-06140dc09e6a`.

## Implementação e critérios de aceite

Modelos aprovados para teste em `automations/partners/templates/modelos.json`, com nove descrições e 54 itens de checklist. Carregamento, preenchimento de marcadores, checklists nativos e snapshots por loja implementados. Edições valem para novas lojas, sem sobrescrita de cards trabalhados. Financeiro já tem lista, campos herdados e coluna no ClickUp, confirmados por consulta.

1. [ ] Conferir automações nativas e template antes da ativação; definir o serviço como único criador das etapas e responsável pelo status deste fluxo. Verificar se o template preenche o nome e se há regras conflitantes; responsáveis, prazos e checklists precisam de mapeamento próprio.
2. [x] Lista/status financeiro preparados e nove destinos/status mapeados explicitamente em `automations/partners/config.py`.
3. [x] Roteamento de parceiros antes dos filtros antigos; pai e etapas restritos às listas autorizadas, fluxo principal preservado.
4. [x] Resposta Sim/Não validada; ausência e alteração posterior bloqueiam para revisão.
5. [x] Criação/reaproveitamento, vínculo, ordem, descrições, checklists e dependências implementados, com detecção de duplicatas.
6. [x] PostgreSQL local no Docker, fila versionada, snapshots, reservas duráveis e advisory lock. Criações sem retry cego; worker separado evita timeout do webhook. Migração futura para Supabase documentada.
7. [x] Status por primeira etapa obrigatória pendente, incluindo financeiro, reabertura e término condicionado a todas as etapas presentes/concluídas.
8. [x] Pré-validação e simulação sem escrita, data de entrada persistida/IDs permitidos e logs por loja; Docker local por consulta periódica, sem alterar o webhook publicado.
9. [ ] Verificação final: testes com cliente simulado para Sim/Não/ausente/índice zero, criação parcial, eventos concorrentes entre processos, resposta de criação perdida, status/reabertura, etapa ausente/arquivada, financeiro e isolamento dos quadros. Rodar `python -m unittest discover -s tests -v`, compilação dos módulos e `git diff --check`. Homologar duas lojas controladas (nova/existente), só depois liberar parceiros; desativar a flag interrompe novas ações, sem desfazer cards criados.

## Limites e próximas decisões

Não foi possível inspecionar automações nativas nem o conteúdo do template pelo conector; conferir eventuais conflitos na homologação controlada. Consulta local a cada 60s faz nova reconciliação, inclusive para dependência removida. Ainda é necessário preencher a credencial local, iniciar o worker e validar duas lojas pelo formulário. Publicação da nova função no Render e Supabase ficam para após esse teste.
