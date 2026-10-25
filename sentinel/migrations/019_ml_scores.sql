-- The machine learning score behind a decision, so the owner sees why Sentinel stopped or allowed a payment.
-- ml_model names the model ('injection' scores seller text for prompt injection, 'wallet_risk' scores an unapproved
-- seller's wallet history), ml_score is its probability of harm (0 to 1) and ml_threshold the cut off it was held to.
-- Null when no model scored the attempt.

alter table payment_attempts add column if not exists ml_model text
    check (ml_model is null or ml_model in ('injection', 'wallet_risk'));
alter table payment_attempts add column if not exists ml_score double precision
    check (ml_score is null or (ml_score >= 0 and ml_score <= 1));
alter table payment_attempts add column if not exists ml_threshold double precision;
