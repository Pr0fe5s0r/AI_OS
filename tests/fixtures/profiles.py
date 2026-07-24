from __future__ import annotations

from typing import Any

# The two domain profiles the test-suite runs the engine against.
#
# Domain knowledge lives ONLY here now: profiles/*.yaml is gone, and the product
# itself learns a company shape through discovery + connector-declared
# capability. These are kept because the engine central claim — that it holds no
# vertical — is only credible if it is exercised against two industries that
# share no vocabulary, no thing types and no rules. Tests are the one place the
# repo allows domain data to be written down.
#
# Generated once from the retired seed files; edit freely, nothing regenerates them.

SOFTWARE_SLOTS: dict[str, Any] = {'company_id': 'default',
 'name': 'Software company',
 'sources': [{'source': 'github',
              'kind': 'connector',
              'connector': 'github',
              'config_fields': [{'key': 'repo',
                                 'label': 'Repository',
                                 'placeholder': 'owner/name'}],
              'token_hint': 'Optional. With a token: private repos, deeper history, real merge '
                            'times.',
              'env_fallback': {'repo': 'GITHUB_REPO',
                               'limit': 'GITHUB_LIMIT',
                               'token': 'GITHUB_TOKEN'},
              'context': {'repo': {'from_config': 'repo', 'op': 'basename'}},
              'mapping': {'id': {'template': 'gh-{repo}-{number}'},
                          'type': {'when_exists': 'pull_request',
                                   'then': {'const': 'pull_request'},
                                   'else': {'const': 'issue'}},
                          'actor_id': {'path': 'user.login', 'default': 'unknown'},
                          'actor_name': {'path': 'user.login', 'default': 'unknown'},
                          'actor_email': {'path': 'user.email'},
                          'timestamp': {'path': 'created_at'},
                          'content': {'concat': [{'path': 'title', 'default': ''},
                                                 {'const': '\n\n'},
                                                 {'path': 'body', 'default': ''}]},
                          'metadata': {'number': {'path': 'number'},
                                       'state': {'path': 'state'},
                                       'url': {'path': 'html_url'},
                                       'labels': {'path': 'labels', 'default': []},
                                       'assignee': {'path': 'assignee.login'},
                                       'comments': {'path': 'comments', 'default': 0},
                                       'created_at': {'path': 'created_at'},
                                       'updated_at': {'path': 'updated_at'},
                                       'closed_at': {'path': 'closed_at'},
                                       'merged_at': {'path': '_merged_at'},
                                       'repo': {'path': '_repo'}}}},
             {'source': 'slack',
              'kind': 'connector',
              'connector': 'slack',
              'config_fields': [{'key': 'channel',
                                 'label': 'Channel',
                                 'placeholder': '#incidents'}],
              'token_hint': 'Required. Bot token (xoxb-…) with channels:history + channels:read.',
              'context': {'channel': {'from_config': 'channel'}},
              'mapping': {'id': {'template': 'slack-{_channel_id}-{ts}'},
                          'type': {'const': 'message'},
                          'actor_id': {'path': 'user', 'default': 'unknown'},
                          'actor_name': {'path': 'user', 'default': 'unknown'},
                          'timestamp': {'epoch': 'ts'},
                          'content': {'path': 'text', 'default': ''},
                          'metadata': {'channel': {'path': '_channel'},
                                       'ts': {'path': 'ts'},
                                       'thread_ts': {'path': 'thread_ts'},
                                       'reactions': {'path': 'reactions', 'default': []}}}},
             {'source': 'zendesk',
              'kind': 'connector',
              'connector': 'zendesk',
              'config_fields': [{'key': 'subdomain', 'label': 'Subdomain', 'placeholder': 'acme'}],
              'token_hint': 'Required. Format: you@company.com/token:API_TOKEN',
              'context': {'subdomain': {'from_config': 'subdomain'}},
              'mapping': {'id': {'template': 'zd-{_subdomain}-{id}'},
                          'type': {'const': 'ticket'},
                          'actor_id': {'path': 'requester_id', 'default': 'unknown'},
                          'actor_name': {'path': 'requester_id', 'default': 'unknown'},
                          'timestamp': {'path': 'created_at'},
                          'content': {'concat': [{'path': 'subject', 'default': ''},
                                                 {'const': '\n\n'},
                                                 {'path': 'description', 'default': ''}]},
                          'metadata': {'ticket_id': {'path': 'id'},
                                       'status': {'path': 'status'},
                                       'priority': {'path': 'priority'},
                                       'tags': {'path': 'tags', 'default': []},
                                       'assignee_id': {'path': 'assignee_id'},
                                       'created_at': {'path': 'created_at'},
                                       'resolved_at': {'path': '_resolved_at'},
                                       'url': {'template': 'https://{subdomain}.zendesk.com/agent/tickets/{id}'}}}}],
 'things': {'types': [{'name': 'Incident', 'source': 'github', 'event_type': 'issue'},
                      {'name': 'PullRequest', 'source': 'github', 'event_type': 'pull_request'},
                      {'name': 'Ticket', 'source': 'zendesk', 'event_type': 'ticket'},
                      {'name': 'Message', 'source': 'slack', 'event_type': 'message'}],
            'default_type': 'Event',
            'status_field': 'state',
            'actor_thing': {'type': 'Person'},
            'entity_rules': {'id_patterns': ['#(\\d+)', '\\b[A-Z]{3,}-\\d+\\b'],
                             'similarity_hard': 0.68,
                             'similarity_soft': 0.63,
                             'candidate_limit': 25,
                             'keyword_rules': [{'name': 'checkout-incident',
                                                'any': ['checkout',
                                                        'pay button',
                                                        'discount code',
                                                        'charged',
                                                        'gift card',
                                                        'promo code',
                                                        'payment failed',
                                                        'failed at checkout',
                                                        'order confirmation']}]}},
 'links': {'types': ['AUTHORED', 'CLOSES', 'IMPLEMENTS', 'MENTIONS', 'SAME_AS', 'CAUSED_BY'],
           'same_as': 'SAME_AS',
           'mentions': 'MENTIONS',
           'authored': 'AUTHORED',
           'closes': {'type': 'CLOSES',
                      'when': {'source_thing': 'PullRequest',
                               'content_pattern': '\\b(fix|fixes|fixed|close[sd]?|resolve[sd]?)\\b',
                               'target_types': ['Incident', 'Feature']}}},
 'rhythms': [{'name': 'issue_resolution_hours',
              'unit': 'hours',
              'window_days': 365,
              'source': 'github',
              'type': 'issue',
              'end_field': 'closed_at'},
             {'name': 'pr_review_hours',
              'unit': 'hours',
              'window_days': 365,
              'source': 'github',
              'type': 'pull_request',
              'end_field': 'merged_at'},
             {'name': 'ticket_resolution_hours',
              'unit': 'hours',
              'window_days': 365,
              'source': 'zendesk',
              'type': 'ticket',
              'end_field': 'resolved_at'}],
 'watchers': [{'name': 'p0_no_owner',
               'title': 'P0 issue with no owner',
               'severity': 'critical',
               'select': {'source': 'github',
                          'type': 'issue',
                          'where': [{'field': 'metadata.state', 'op': 'eq', 'value': 'open'},
                                    {'field': 'metadata.labels',
                                     'op': 'contains_any',
                                     'value': ['p0', 'critical', 'urgent', 'sev1', 'blocker']},
                                    {'field': 'metadata.assignee', 'op': 'is_null'}]},
               'llm': True,
               'prompt': 'p0_no_owner',
               'limit': 3},
              {'name': 'unassigned_bug',
               'title': 'Unassigned bug report',
               'severity': 'high',
               'select': {'source': 'github',
                          'type': 'issue',
                          'where': [{'field': 'metadata.state', 'op': 'eq', 'value': 'open'},
                                    {'field': 'metadata.labels',
                                     'op': 'contains_any',
                                     'value': ['bug', 'defect', 'regression']},
                                    {'field': 'metadata.assignee', 'op': 'is_null'}]},
               'llm': True,
               'prompt': 'unassigned_bug',
               'limit': 3},
              {'name': 'needs_owner',
               'title': 'Open issue with no owner',
               'severity': 'high',
               'select': {'source': 'github',
                          'type': 'issue',
                          'where': [{'field': 'metadata.state', 'op': 'eq', 'value': 'open'},
                                    {'field': 'metadata.assignee', 'op': 'is_null'}]},
               'llm': True,
               'prompt': 'needs_owner',
               'limit': 5},
              {'name': 'untriaged_issue',
               'title': 'Open issue with no triage',
               'severity': 'medium',
               'select': {'source': 'github',
                          'type': 'issue',
                          'where': [{'field': 'metadata.state', 'op': 'eq', 'value': 'open'},
                                    {'field': 'metadata.assignee', 'op': 'not_null'},
                                    {'field': 'metadata.labels', 'op': 'is_null'}]},
               'llm': True,
               'prompt': 'untriaged_issue',
               'limit': 5},
              {'name': 'sla_breach_risk',
               'title': 'Issue open far longer than normal',
               'severity': 'high',
               'select': {'source': 'github',
                          'type': 'issue',
                          'where': [{'field': 'metadata.state', 'op': 'eq', 'value': 'open'}]},
               'norm': {'metric': 'issue_resolution_hours', 'k': 2.0},
               'llm': True,
               'prompt': 'sla_breach_risk',
               'limit': 3},
              {'name': 'stale_open_issue',
               'title': 'Stale open issue',
               'severity': 'medium',
               'recommended_action': 'Triage or close: no activity in over 30 days.',
               'select': {'source': 'github',
                          'type': 'issue',
                          'where': [{'field': 'metadata.state', 'op': 'eq', 'value': 'open'},
                                    {'field': 'timestamp', 'op': 'older_than_days', 'value': 30}]},
               'llm': False,
               'limit': 5},
              {'name': 'spec_drift',
               'title': 'Pull request closes no tracked issue',
               'severity': 'medium',
               'graph': {'thing_type': 'PullRequest', 'missing_link_type': 'CLOSES'},
               'llm': True,
               'prompt': 'spec_drift',
               'limit': 3}],
 'moves': {'registry': {'apply_label': {'kind': 'http',
                                        'approval_required': False,
                                        'method': 'POST',
                                        'url': 'https://api.github.com/repos/{repo}/issues/{number}/labels',
                                        'auth': {'source': 'github'},
                                        'argument': 'ONE GitHub label name (e.g. bug, enhancement, '
                                                    'question)',
                                        'params': {'body': {'labels': ['{argument}']},
                                                   'target': 'github_issue'},
                                        'default_argument': 'triage'},
                        'assign_issue': {'kind': 'http',
                                         'approval_required': False,
                                         'method': 'POST',
                                         'url': 'https://api.github.com/repos/{repo}/issues/{number}/assignees',
                                         'auth': {'source': 'github'},
                                         'argument': 'ONE GitHub username to assign',
                                         'params': {'body': {'assignees': ['{argument}']},
                                                    'target': 'github_issue'}},
                        'draft_reply': {'kind': 'log',
                                        'approval_required': False,
                                        'template': 'DRAFT reply prepared for {situation_id}',
                                        'argument': 'the reply text'},
                        'close_issue': {'kind': 'http',
                                        'approval_required': False,
                                        'method': 'PATCH',
                                        'url': 'https://api.github.com/repos/{repo}/issues/{number}',
                                        'auth': {'source': 'github'},
                                        'params': {'body': {'state': 'closed'},
                                                   'target': 'github_issue'}},
                        'comment_on_pr': {'kind': 'http',
                                          'approval_required': True,
                                          'method': 'POST',
                                          'url': 'https://api.github.com/repos/{repo}/issues/{number}/comments',
                                          'auth': {'source': 'github'},
                                          'argument': 'comment text posted on the EXISTING '
                                                      'issue/PR (public, needs human approval). '
                                                      'Never propose opening a new issue.',
                                          'params': {'body': {'body': '{argument}'},
                                                     'target': 'github_issue'},
                                          'default_argument': '**AI analysis** ({severity})\n'
                                                              '\n'
                                                              '{summary}\n'
                                                              '\n'
                                                              '**Suggested next step:** '
                                                              '{recommended_action}'},
                        'page_engineer': {'kind': 'log',
                                          'approval_required': True,
                                          'template': 'PAGE on-call engineer about {situation_id}',
                                          'argument': 'why the on-call must wake up (needs human '
                                                      'approval)'}},
           'target_url_pattern': 'github\\.com/([^/]+/[^/]+)/(?:issues|pull)/(\\d+)',
           'target_params': ['repo', 'number'],
           'autonomy': {'enabled': True,
                        'min_confidence': 0.7,
                        'escalate_severities': ['critical'],
                        'allowed_actions': ['apply_label', 'assign_issue', 'draft_reply'],
                        'assignment_requires_human': False,
                        'escalation_action': 'page_engineer',
                        'max_actions_per_run': 5},
           'approval_defaults': {'default_require_approval': False, 'dry_run': True},
           'team': {'roles': ['project_manager', 'team_lead', 'engineer'],
                    'notify_roles': ['project_manager', 'team_lead'],
                    'assignable_role': 'engineer',
                    'assign_move': 'assign_issue',
                    'close_move': 'close_issue',
                    'workload': {'source': 'github',
                                 'type': 'issue',
                                 'state_field': 'state',
                                 'state_value': 'open',
                                 'assignee_field': 'assignee'}}},
 'vocabulary': {'terms': {'thing': 'issue',
                          'situation': 'flag',
                          'actor': 'engineer',
                          'workspace': 'repo'},
                'briefing_policy': [{'mode': 'waiting_for_signal',
                                     'when': [{'field': 'sources_connected',
                                               'op': 'eq',
                                               'value': 0}],
                                     'headline': 'Connect a source so the OS can start watching '
                                                 'real work.',
                                     'next_best_action': 'Add a GitHub, Slack, or Zendesk '
                                                         'connection, then scan the workspace.'},
                                    {'mode': 'waiting_for_sync',
                                     'when': [{'field': 'events', 'op': 'eq', 'value': 0}],
                                     'headline': 'Connected, but nothing has been pulled in yet.',
                                     'next_best_action': 'Hit Sync on your connection, then scan '
                                                         'the workspace.'},
                                    {'mode': 'needs_human',
                                     'when': [{'field': 'pending_approvals',
                                               'op': 'gt',
                                               'value': 0}],
                                     'headline': '{pending_approvals} action{pending_approvals_s} '
                                                 'waiting for approval.',
                                     'next_best_action': 'Open the command center and approve, '
                                                         'reject, or inspect the prepared action.'},
                                    {'mode': 'triage_now',
                                     'when': [{'field': 'high_risk', 'op': 'gt', 'value': 0}],
                                     'headline': '{high_risk} high-priority situation{high_risk_s} '
                                                 'to triage.',
                                     'next_best_action': 'Open Flags and review the prepared next '
                                                         'action.'},
                                    {'mode': 'triage_queue',
                                     'when': [{'field': 'active_situations',
                                               'op': 'gt',
                                               'value': 0}],
                                     'headline': '{active_situations} '
                                                 'situation{active_situations_s} ready for triage.',
                                     'next_best_action': 'Review the evidence chain and choose an '
                                                         'action.'},
                                    {'mode': 'monitoring',
                                     'when': [],
                                     'headline': 'No active risks in the current signal set.',
                                     'next_best_action': 'Keep monitoring, or scan again after the '
                                                         'next sync.'}],
                'prompts': {'p0_no_owner': 'You triage engineering incidents for a software '
                                           'company.\n'
                                           'A P0-labelled GitHub issue has no assignee.\n'
                                           '\n'
                                           'Title rule: {title}\n'
                                           'Source: {source}\n'
                                           'Author: {actor}\n'
                                           'Issue content:\n'
                                           '{content}\n'
                                           '\n'
                                           'Metadata: {metadata}\n'
                                           'Team norms: {norms}\n'
                                           '\n'
                                           'Classify the severity, write a two-sentence summary of '
                                           'the risk, and give one concrete recommended action '
                                           'naming who should pick it up.\n',
                            'unassigned_bug': 'You triage bug reports for a software company.\n'
                                              'An open bug has no assignee.\n'
                                              '\n'
                                              'Source: {source}\n'
                                              'Author: {actor}\n'
                                              'Issue content:\n'
                                              '{content}\n'
                                              '\n'
                                              'Metadata: {metadata}\n'
                                              'Team norms: {norms}\n'
                                              '\n'
                                              'Classify severity from real user impact, summarize '
                                              'in two sentences, and recommend one action.\n',
                            'untriaged_issue': 'You triage the inbound issue queue for a software '
                                               'team.\n'
                                               'This GitHub issue is open and has never been '
                                               'labelled — nobody has classified it.\n'
                                               '\n'
                                               'Source: {source}\n'
                                               'Author: {actor}\n'
                                               'Issue content:\n'
                                               '{content}\n'
                                               '\n'
                                               'Metadata: {metadata}\n'
                                               'Team norms: {norms}\n'
                                               '\n'
                                               'Judge severity from the real user impact described '
                                               '(a broken API or crash is high; a feature request '
                                               'or chore is low). Summarize the risk in two '
                                               'sentences. Recommend one concrete next step: the '
                                               'label to apply, and who should own it (note the '
                                               'assignee in the metadata if one is already set).\n',
                            'sla_breach_risk': 'You monitor delivery risk for a software company.\n'
                                               'This issue has been open far longer than the '
                                               "team's normal resolution time.\n"
                                               '\n'
                                               'Source: {source}\n'
                                               'Author: {actor}\n'
                                               'Issue content:\n'
                                               '{content}\n'
                                               '\n'
                                               'Metadata: {metadata}\n'
                                               'Team norms (baselines): {norms}\n'
                                               '\n'
                                               'Judge how serious the delay is, summarize in two '
                                               'sentences citing the norm, and recommend one '
                                               'action.\n',
                            'needs_owner': 'You triage the inbound issue queue for a software '
                                           'team.\n'
                                           'This GitHub issue is open and nobody has been assigned '
                                           'to it.\n'
                                           '\n'
                                           'Source: {source}\n'
                                           'Author: {actor}\n'
                                           'Issue content:\n'
                                           '{content}\n'
                                           '\n'
                                           'Metadata: {metadata}\n'
                                           'Team norms: {norms}\n'
                                           '\n'
                                           'Judge severity from the real user impact described. '
                                           'Summarize the problem in two sentences so a project '
                                           'manager can decide who should pick it up. Recommend '
                                           'what kind of engineer should own it.\n',
                            'choose_assignee': 'You assign engineering work for a software team.\n'
                                               'A situation needs an owner. Pick the single best '
                                               'person from the AVAILABLE engineers below — they '
                                               'all have spare capacity right now.\n'
                                               '\n'
                                               'Situation: {title}\n'
                                               'Severity: {severity}\n'
                                               'Summary: {summary}\n'
                                               '\n'
                                               'Evidence:\n'
                                               '{evidence}\n'
                                               '\n'
                                               'Available engineers (id, name, roles, skills, '
                                               'current workload / capacity):\n'
                                               '{candidates}\n'
                                               '\n'
                                               'Match the work to skills, and prefer whoever has '
                                               "the lightest workload. Answer with that person's "
                                               "id. Answer 'none' only if nobody listed can "
                                               'plausibly do this work.\n'
                                               'Set confidence between 0 and 1.\n',
                            'choose_action': 'You are the autonomous operator of a software '
                                             "company's engineering workspace.\n"
                                             'A situation has been detected. Decide the single '
                                             'best action to resolve it.\n'
                                             '\n'
                                             'Situation: {title}\n'
                                             'Severity: {severity}\n'
                                             'Summary: {summary}\n'
                                             'Prior recommendation: {recommended_action}\n'
                                             '\n'
                                             'Evidence:\n'
                                             '{evidence}\n'
                                             '\n'
                                             'You may choose exactly one of these actions: '
                                             '{actions}\n'
                                             '\n'
                                             'Meaning of each action and what `argument` must '
                                             'contain:\n'
                                             '{action_help}\n'
                                             '\n'
                                             'ACT. You are trusted to handle this yourself, and '
                                             'every action above is reversible in one click — a '
                                             'wrong label costs seconds to undo, while doing '
                                             'nothing lets the work rot.\n'
                                             '\n'
                                             "Choose 'escalate' ONLY when acting could cause real "
                                             'harm that cannot be undone, or when the decision '
                                             'needs information you genuinely do not have. '
                                             'Escalating wakes a human being, so treat it as the '
                                             'expensive last resort it is — not as the safe '
                                             'default. "I am not certain" is NOT a reason to '
                                             'escalate: pick the smallest sensible action '
                                             'instead.\n'
                                             'Set confidence between 0 and 1: how sure you are '
                                             'this is the right action.\n',
                            'spec_drift': 'You audit engineering traceability.\n'
                                          'This pull request is not linked to any tracked issue it '
                                          'closes, so shipped work may not match a spec.\n'
                                          '\n'
                                          'PR content:\n'
                                          '{content}\n'
                                          '\n'
                                          'Metadata: {metadata}\n'
                                          '\n'
                                          'Judge the risk, summarize in two sentences, and '
                                          'recommend one action.\n'}}}


INVENTORY_SLOTS: dict[str, Any] = {'company_id': 'acme-inventory',
 'name': 'Acme Inventory Ops',
 'sources': [{'source': 'ops',
              'kind': 'push',
              'mapping': {'id': {'template': 'ops-{ref}'},
                          'type': {'path': 'kind', 'default': 'event'},
                          'actor_id': {'path': 'actor.id', 'default': 'unknown'},
                          'actor_name': {'path': 'actor.name', 'default': 'unknown'},
                          'timestamp': {'path': 'occurred_at'},
                          'content': {'concat': [{'path': 'title', 'default': ''},
                                                 {'const': '\n\n'},
                                                 {'path': 'notes', 'default': ''}]},
                          'metadata': {'ref': {'path': 'ref'},
                                       'status': {'path': 'status'},
                                       'supplier': {'path': 'supplier'},
                                       'sku': {'path': 'sku'},
                                       'quantity': {'path': 'quantity'},
                                       'expected_at': {'path': 'expected_at'},
                                       'delivered_at': {'path': 'delivered_at'},
                                       'warehouse': {'path': 'warehouse'}}}}],
 'things': {'types': [{'name': 'PurchaseOrder', 'source': 'ops', 'event_type': 'purchase_order'},
                      {'name': 'Delivery', 'source': 'ops', 'event_type': 'delivery'},
                      {'name': 'StockCount', 'source': 'ops', 'event_type': 'stock_count'},
                      {'name': 'SupplierNote', 'source': 'ops', 'event_type': 'supplier_message'}],
            'default_type': 'Event',
            'status_field': 'status',
            'actor_thing': {'type': 'Contact'},
            'entity_rules': {'id_patterns': ['\\bPO-\\d+\\b'],
                             'similarity_hard': 0.7,
                             'similarity_soft': 0.6,
                             'candidate_limit': 25,
                             'keyword_rules': [{'name': 'cold-chain',
                                                'any': ['refrigerated',
                                                        'cold chain',
                                                        'temperature',
                                                        'spoilage',
                                                        'perishable']}]}},
 'links': {'types': ['AUTHORED', 'FULFILLS', 'MENTIONS', 'SAME_AS', 'DELAYED_BY'],
           'same_as': 'SAME_AS',
           'mentions': 'MENTIONS',
           'authored': 'AUTHORED',
           'closes': {'type': 'FULFILLS',
                      'when': {'source_thing': 'Delivery',
                               'content_pattern': '\\b(delivered|received|fulfill(?:s|ed)?|arrived)\\b',
                               'target_types': ['PurchaseOrder']}}},
 'rhythms': [{'name': 'po_fulfillment_hours',
              'unit': 'hours',
              'window_days': 180,
              'source': 'ops',
              'type': 'purchase_order',
              'end_field': 'delivered_at'},
             {'name': 'supplier_reply_hours',
              'unit': 'hours',
              'window_days': 90,
              'source': 'ops',
              'type': 'supplier_message',
              'end_field': 'delivered_at'}],
 'watchers': [{'name': 'overdue_po',
               'title': 'Purchase order past its expected date',
               'severity': 'high',
               'select': {'source': 'ops',
                          'type': 'purchase_order',
                          'where': [{'field': 'metadata.status', 'op': 'eq', 'value': 'open'},
                                    {'field': 'metadata.expected_at',
                                     'op': 'older_than_days',
                                     'value': 0}]},
               'llm': True,
               'prompt': 'overdue_po',
               'limit': 5},
              {'name': 'low_stock',
               'title': 'Stock below reorder point',
               'severity': 'critical',
               'select': {'source': 'ops',
                          'type': 'stock_count',
                          'where': [{'field': 'metadata.quantity', 'op': 'lt', 'value': 10}]},
               'llm': True,
               'prompt': 'low_stock',
               'limit': 5},
              {'name': 'po_slower_than_normal',
               'title': "PO open far longer than this supplier's normal",
               'severity': 'high',
               'select': {'source': 'ops',
                          'type': 'purchase_order',
                          'where': [{'field': 'metadata.status', 'op': 'eq', 'value': 'open'}]},
               'norm': {'metric': 'po_fulfillment_hours', 'k': 2.0},
               'llm': True,
               'prompt': 'overdue_po',
               'limit': 3},
              {'name': 'unfulfilled_po',
               'title': 'Purchase order with no linked delivery',
               'severity': 'medium',
               'graph': {'thing_type': 'PurchaseOrder', 'missing_link_type': 'FULFILLS'},
               'llm': False,
               'recommended_action': 'Chase the supplier for a delivery confirmation.',
               'limit': 5}],
 'moves': {'registry': {'notify_supplier': {'kind': 'log',
                                            'approval_required': False,
                                            'template': 'NOTIFY supplier about {situation_id}',
                                            'argument': 'what to tell the supplier'},
                        'draft_reply': {'kind': 'log',
                                        'approval_required': False,
                                        'template': 'DRAFT reply prepared for {situation_id}',
                                        'argument': 'the reply text'},
                        'reorder_stock': {'kind': 'log',
                                          'approval_required': True,
                                          'template': 'REORDER stock for {situation_id}',
                                          'argument': 'SKU and quantity to reorder'},
                        'escalate_purchasing': {'kind': 'log',
                                                'approval_required': True,
                                                'template': 'ESCALATE to purchasing lead: '
                                                            '{situation_id}',
                                                'argument': 'why purchasing must decide now'}},
           'autonomy': {'enabled': True,
                        'min_confidence': 0.7,
                        'escalate_severities': ['critical'],
                        'allowed_actions': ['notify_supplier', 'draft_reply', 'reorder_stock'],
                        'assignment_requires_human': False,
                        'escalation_action': 'escalate_purchasing',
                        'max_actions_per_run': 5},
           'approval_defaults': {'default_require_approval': False, 'dry_run': True},
           'team': {'roles': ['ops_manager', 'buyer', 'warehouse'],
                    'notify_roles': ['ops_manager'],
                    'assignable_role': 'buyer',
                    'workload': {'source': 'ops',
                                 'type': 'purchase_order',
                                 'state_field': 'status',
                                 'state_value': 'open',
                                 'assignee_field': 'assignee'}}},
 'vocabulary': {'terms': {'thing': 'order',
                          'situation': 'supply risk',
                          'actor': 'supplier',
                          'workspace': 'warehouse'},
                'briefing_policy': [{'mode': 'waiting_for_signal',
                                     'when': [{'field': 'events', 'op': 'eq', 'value': 0}],
                                     'headline': 'No inventory signals yet.',
                                     'next_best_action': 'Push purchase orders, deliveries and '
                                                         'stock counts into the ops feed.'},
                                    {'mode': 'needs_human',
                                     'when': [{'field': 'pending_approvals',
                                               'op': 'gt',
                                               'value': 0}],
                                     'headline': '{pending_approvals} purchasing '
                                                 'action{pending_approvals_s} waiting for '
                                                 'approval.',
                                     'next_best_action': 'Approve or reject the prepared reorder.'},
                                    {'mode': 'triage_now',
                                     'when': [{'field': 'high_risk', 'op': 'gt', 'value': 0}],
                                     'headline': '{high_risk} supply risk{high_risk_s} need '
                                                 'attention.',
                                     'next_best_action': 'Review overdue orders and low stock.'},
                                    {'mode': 'monitoring',
                                     'when': [],
                                     'headline': 'Supply chain is inside its normal rhythm.',
                                     'next_best_action': 'Keep monitoring deliveries against '
                                                         'expected dates.'}],
                'prompts': {'overdue_po': 'You monitor supply risk for an inventory operation.\n'
                                          'A purchase order is past its expected delivery date (or '
                                          "far slower than this supplier's norm).\n"
                                          '\n'
                                          'Source: {source}\n'
                                          'Supplier contact: {actor}\n'
                                          'Order details:\n'
                                          '{content}\n'
                                          '\n'
                                          'Metadata: {metadata}\n'
                                          'Supply norms: {norms}\n'
                                          '\n'
                                          'Judge how serious the delay is for stock availability, '
                                          'summarize in two sentences, and recommend one concrete '
                                          'action (chase, reorder elsewhere, or escalate).\n',
                            'low_stock': 'You monitor stock levels for an inventory operation.\n'
                                         'A stock count came in below the reorder point.\n'
                                         '\n'
                                         'Source: {source}\n'
                                         'Reported by: {actor}\n'
                                         'Count details:\n'
                                         '{content}\n'
                                         '\n'
                                         'Metadata: {metadata}\n'
                                         'Supply norms: {norms}\n'
                                         '\n'
                                         'Judge how urgent the shortage is, summarize in two '
                                         'sentences, and recommend one concrete action naming the '
                                         'SKU and quantity.\n',
                            'choose_assignee': 'You assign purchasing work for an inventory team.\n'
                                               'A supply risk needs an owner. Pick the single best '
                                               'person from the AVAILABLE buyers below — they all '
                                               'have spare capacity right now.\n'
                                               '\n'
                                               'Situation: {title}\n'
                                               'Severity: {severity}\n'
                                               'Summary: {summary}\n'
                                               '\n'
                                               'Evidence:\n'
                                               '{evidence}\n'
                                               '\n'
                                               'Available buyers (id, name, roles, skills, current '
                                               'workload / capacity):\n'
                                               '{candidates}\n'
                                               '\n'
                                               'Match the work to skills, and prefer whoever has '
                                               "the lightest workload. Answer with that person's "
                                               "id. Answer 'none' only if nobody listed can "
                                               'plausibly do this work.\n'
                                               'Set confidence between 0 and 1.\n',
                            'choose_action': 'You are the autonomous operator of an inventory '
                                             'workspace.\n'
                                             'A supply risk has been detected. Decide the single '
                                             'best action to resolve it.\n'
                                             '\n'
                                             'Situation: {title}\n'
                                             'Severity: {severity}\n'
                                             'Summary: {summary}\n'
                                             'Prior recommendation: {recommended_action}\n'
                                             '\n'
                                             'Evidence:\n'
                                             '{evidence}\n'
                                             '\n'
                                             'You may choose exactly one of these actions: '
                                             '{actions}\n'
                                             '\n'
                                             'Meaning of each action and what `argument` must '
                                             'contain:\n'
                                             '{action_help}\n'
                                             '\n'
                                             'Prefer the smallest reversible action that moves the '
                                             "work forward. Choose 'escalate' ONLY if a human "
                                             'judgement call is genuinely required and no action '
                                             'above is safe.\n'
                                             'Set confidence between 0 and 1: how sure you are '
                                             'this is the right action.\n'}}}


