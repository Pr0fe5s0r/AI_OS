# Thin orchestration shared by the API and the worker. Loads profile DATA from
# Postgres and passes it into core functions as arguments. No business logic
# beyond sequencing; no domain knowledge beyond what the profile row says.
