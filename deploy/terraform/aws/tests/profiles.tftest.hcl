# Offline checks of the sizing profiles and hardening (no AWS account needed):
#   terraform -chdir=deploy/terraform/aws init -backend=false && terraform -chdir=... test
mock_provider "aws" {
  override_data {
    target = data.aws_availability_zones.available
    values = { names = ["mx-central-1a", "mx-central-1b"] }
  }
  override_data {
    target = data.aws_iam_policy_document.ecs_assume
    values = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }
}

variables {
  name         = "clinica-sonrisa-appointments"
  tenant_id    = "clinica-sonrisa"
  region       = "mx-central-1"
  image_tag    = "1.0.0-abc123def456"
  secret_names = ["anthropic", "twilio"]
}

run "small_is_cheap_and_public" {
  command = plan
  assert {
    condition     = aws_ecs_service.instance.desired_count == 1 && aws_db_instance.main.instance_class == "db.t4g.micro"
    error_message = "small: one task on db.t4g.micro"
  }
  assert {
    condition     = length(aws_nat_gateway.main) == 0 && aws_db_instance.main.multi_az == false
    error_message = "small: no NAT, single-AZ database"
  }
  assert {
    condition     = aws_ecs_service.instance.deployment_minimum_healthy_percent == 100 && aws_ecs_service.instance.enable_execute_command == false
    error_message = "rolling upgrades keep the old task until the new one is healthy; no exec into prod"
  }
  assert {
    condition     = aws_ecs_service.instance.deployment_circuit_breaker[0].rollback
    error_message = "a failing release rolls back"
  }
  assert {
    condition     = aws_db_instance.main.storage_encrypted && !aws_db_instance.main.publicly_accessible && aws_db_instance.main.deletion_protection
    error_message = "the database is encrypted, private and protected"
  }
  assert {
    condition     = length(aws_cloudwatch_metric_alarm.instance) == 6 && length(aws_sns_topic_subscription.email) == 0
    error_message = "six alarms; no subscriber without alarm_email"
  }
}

run "large_is_redundant_and_private" {
  command = plan
  variables {
    size        = "large"
    alarm_email = "oncall@example.com"
    fleet_url   = "https://fleet.di-factory.biz"
  }
  assert {
    condition     = aws_ecs_service.instance.desired_count == 2 && aws_db_instance.main.multi_az
    error_message = "large: two tasks and a Multi-AZ database"
  }
  assert {
    condition     = length(aws_nat_gateway.main) == 1 && aws_ecs_service.instance.network_configuration[0].assign_public_ip == false
    error_message = "large: tasks in private subnets behind NAT"
  }
  assert {
    condition     = aws_db_instance.main.backup_retention_period == 30 && aws_db_instance.main.performance_insights_enabled
    error_message = "large: 30-day backups and Performance Insights"
  }
  assert {
    condition     = length(local.optional_env) == 1 && local.optional_env[0].name == "DIF_FLEET_URL"
    error_message = "the instance agent is configured when fleet_url is set"
  }
  assert {
    condition     = length(aws_sns_topic_subscription.email) == 1
    error_message = "alarms go to the on-call address"
  }
}

run "unknown_size_is_refused" {
  command = plan
  variables {
    size = "huge"
  }
  expect_failures = [var.size]
}
