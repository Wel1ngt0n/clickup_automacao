# Modelos de cards de parceiros

Modelos iniciais aprovados para teste do processo interno. O processo local lê este catálogo e cria as tarefas com descrições e checklists nativos do ClickUp. Não são templates salvos na biblioteca do ClickUp: são a configuração da automação deste projeto. Alterar `enabled` sozinho não inicia o processo; veja o guia de Docker no README principal.

## Onde editar

- `modelos.json`: nome do card, lista de destino, status, responsáveis, prazo e checklists de cada etapa.
- `01-onboarding.md` a `09-pos-implantacao.md`: descrição com o que fazer, como agir e critério de conclusão. Os textos são sugestões iniciais para revisão do time.

Em cada etapa de `modelos.json`, edite `name_template`, `assignee_ids`, `due_after_days` e `checklists`. Os responsáveis usam IDs de usuários do ClickUp; lista vazia significa sem responsável predefinido. `due_after_days: null` significa sem prazo automático; um número inteiro não negativo representará dias após a criação do card. Os valores em `defaults` serão o padrão quando o campo for omitido na etapa; lista vazia e `null` explícitos mantêm a opção sem responsável ou prazo.

## Responsáveis iniciais

| Etapa | Responsável |
| --- | --- |
| Onboarding | Implantador do principal |
| Criar Loja | Implantador do principal |
| Integração ERP/PDV | Guilherme Henrique Oliveira Sotério |
| Integração Marketplace | Welington |
| Cadastro de produtos | Implantador do principal, somente cliente novo |
| Qualidade | Welington |
| Treinamento | Implantador do principal |
| Configuração financeira | Vanessa Santos Gomes |
| Pós-Implantação | Implantador do principal |

`assignee_source: "parent"` herda os responsáveis do card principal; `"fixed"`
usa `assignee_ids`. Se o principal ainda estiver sem responsável, as etapas são
criadas normalmente e recebem a atribuição quando o responsável for definido.
Havendo mais de um responsável no principal, todos são herdados inicialmente.

A atribuição é inicial, não uma trava. Depois de registrada, a automação preserva
trocas e remoções manuais: Guilherme pode delegar a outro integrador sem ser
reatribuído pelo serviço. Trocar o implantador no principal posteriormente não
sobrescreve as etapas já atribuídas. Cards abertos já criados e ainda sem
atribuição inicial também recebem essas regras; cards concluídos ficam intactos.
Se uma atualização não puder ser confirmada, a pendência aparece nos logs para
revisão em vez de repetir a atribuição sem confirmação.

Durante o teste privado, `CLICKUP_PARTNER_ALLOWED_ASSIGNEE_IDS=61029153` no `.env`
restringe as atribuições automáticas à conta de Welington. Os demais responsáveis
continuam definidos no catálogo, mas não são enviados como responsáveis na
criação nem atribuídos depois. O serviço não compartilha listas, pasta ou espaço.
Ao liberar o quadro para o time, revisar acessos e essa restrição antes de ativar
as atribuições completas; tarefas com atribuição anterior não confirmada exigem
conferência, sem apagar suas reservas para forçar uma tentativa.

Cada checklist tem `name` e `items`. Altere, adicione ou remova os textos de `items` na ordem desejada; todos os itens deverão nascer desmarcados quando a criação dos cards for implementada. É possível adicionar mais de um checklist por etapa.

Exemplo de edição:

```json
"checklists": [
  {
    "name": "Conferência do time",
    "items": [
      "Conferir os dados recebidos",
      "Registrar a aprovação do responsável"
    ]
  }
]
```

O arquivo JSON deve permanecer válido: use aspas duplas e não inclua comentários ou vírgula após o último item. Os arquivos de descrição são resolvidos a partir desta pasta.

## Informações variáveis do projeto

Os nomes e descrições usam estes marcadores, que a futura automação preencherá:

| Marcador | Origem |
| --- | --- |
| `{{nome_loja}}` | Campo Nome da loja no card principal |
| `{{id_tarefa_pai}}` | ID interno do card principal |
| `{{url_card_principal}}` | Link do card principal |

Os campos de vínculo e ordem serão preenchidos pela automação, conforme os IDs em `fields`. Ao editar o conteúdo, preserve `key`, `order`, `list_id`, `parent_status` e `applies_to`, pois definem o fluxo e devem ser alterados junto com suas regras e testes.

## Revisão antes de ativar

O catálogo e cada etapa estão com `review_status: "approved"` após a aprovação dos modelos iniciais para teste. Uma etapa em `draft` bloqueia o início do processo até a revisão. Isso registra a revisão; não publica cards por conta própria. O banco guarda uma cópia dos modelos quando cada loja entra no fluxo: alterações posteriores valem para novas lojas, sem sobrescrever descrições ou checklists já trabalhados.

Cadastro de produtos tem `applies_to: "new_clients"`: cliente existente deve pular a etapa inteira, incluindo seu checklist. Não há tarefa de replicação nem cópia de produtos. Resposta ausente no formulário deverá ser tratada como pendência, não como cliente novo.

Configuração financeira usa a lista `1400600000003634`, já criada na pasta de parceiros, e o status `configuração financeira` no principal. O catálogo está com `enabled: true`, mas o serviço exige também `CLICKUP_PARTNER_ENABLED=true`, acesso ao banco e limite de entrada por data/IDs. No Render, mantenha parceiros desabilitados enquanto o teste roda no Docker local.
