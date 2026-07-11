"""Trained decision-policy stack for the Timberborn agent.

Replicates the shasankp000/NLP_2.0_pipeline approach (LIDSNet MLP + CART tree over
symbolic features, LLM tie-break) but the "utterance" is the game STATE and the
"intent" is the planner's next goal_id. Placement (WHERE) stays in placement.py;
these models only decide WHAT. See docs/kb/nlp-pipeline-replication.md.

Modules:
  features.py  - StateFeaturizer: state dict -> symbolic feature strings -> vector
  labeler.py   - expert oracle (behavioral cloning from the deterministic planner)
  dataset.py   - synthetic + journal state sweep -> labeled dataset + vocab
  train_lidsnet.py / train_cart.py - the two learned heads
  policy.py    - inference: featurize -> heads -> (agree? act : LLM arbitrate)
"""
