output "url" {
  description = "Where the instance answers (point the client's domain here)."
  value       = "${local.https ? "https" : "http"}://${aws_lb.main.dns_name}"
}

output "ecr_repository" {
  value = aws_ecr_repository.instance.repository_url
}

output "secrets_to_fill" {
  description = "Secrets Manager entries the client must set before the service starts."
  value       = [for s in aws_secretsmanager_secret.instance : s.name]
}

output "database_endpoint" {
  value = aws_db_instance.main.address
}

output "alarm_topic" {
  description = "SNS topic for the instance's alarms (subscribe the client's on-call here)."
  value       = aws_sns_topic.alarms.arn
}

output "image_tag" {
  description = "The running release; to roll back, apply again with the previous image_tag."
  value       = var.image_tag
}
