# AWS deploy for one dif-general-harness instance (ARCHITECTURE §3.20; basic in M2, hardened
# in M4): one region, ECS Fargate behind an ALB, RDS Postgres, Secrets Manager, CloudWatch
# alarms. The client owns the account; secret values are set by the client in Secrets Manager.
#
# Upgrades: a new image_tag rolls the service task by task; a release that fails its health
# checks is rolled back by the deployment circuit breaker, and earlier images stay in ECR
# (images_to_keep) for a manual rollback (apply with the previous image_tag). Config changes
# go through the instance's versioned config (admin API or the instance agent), not Terraform.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      "dif:instance" = var.name
      "dif:tenant"   = var.tenant_id
      "managed-by"   = "dif-general-harness"
    }
  }
}

locals {
  prefix = "dif/${var.tenant_id}/${var.name}"
  sizes = {
    small  = { cpu = 512, memory = 1024, tasks = 1, db = "db.t4g.micro", multi_az = false, backups = 7, storage = 20, insights = false, private = false }
    medium = { cpu = 1024, memory = 2048, tasks = 1, db = "db.t4g.small", multi_az = false, backups = 14, storage = 50, insights = false, private = false }
    large  = { cpu = 2048, memory = 4096, tasks = 2, db = "db.t4g.medium", multi_az = true, backups = 30, storage = 100, insights = true, private = true }
  }
  size    = local.sizes[var.size]
  https   = var.certificate_arn != ""
  private = coalesce(var.private_tasks, local.size.private)
  optional_env = concat(
    var.fleet_url != "" ? [
      { name = "DIF_FLEET_URL", value = var.fleet_url },
      { name = "DIF_FLEET_PUBLIC_KEY", value = var.fleet_public_key },
    ] : [],
    var.otel_endpoint != "" ? [{ name = "OTEL_EXPORTER_OTLP_ENDPOINT", value = var.otel_endpoint }] : [],
  )
}

data "aws_availability_zones" "available" {
  state = "available"
}

# --- network -------------------------------------------------------------------------

resource "aws_vpc" "main" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_hostnames = true
  tags                 = { Name = var.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.name}-public-${count.index}" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, count.index + 10)
  availability_zone = data.aws_availability_zones.available.names[count.index]
  tags              = { Name = "${var.name}-private-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}

resource "aws_route_table_association" "public" {
  count          = 2
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

resource "aws_eip" "nat" {
  count  = local.private ? 1 : 0
  domain = "vpc"
}

resource "aws_nat_gateway" "main" {
  count         = local.private ? 1 : 0
  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "private" {
  count  = local.private ? 1 : 0
  vpc_id = aws_vpc.main.id
  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main[0].id
  }
}

resource "aws_route_table_association" "private" {
  count          = local.private ? 2 : 0
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[0].id
}

resource "aws_security_group" "alb" {
  name   = "${var.name}-alb"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "app" {
  name   = "${var.name}-app"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }
  egress { # model providers, channel APIs, the control plane: outbound https only needs this
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group" "db" {
  name   = "${var.name}-db"
  vpc_id = aws_vpc.main.id
  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [aws_security_group.app.id]
  }
}

# --- image and secrets ---------------------------------------------------------------

resource "aws_ecr_repository" "instance" {
  name                 = "dif/${var.name}"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "instance" {
  repository = aws_ecr_repository.instance.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last ${var.images_to_keep} images for rollback"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = var.images_to_keep }
      action       = { type = "expire" }
    }]
  })
}

resource "aws_secretsmanager_secret" "instance" {
  for_each    = toset(var.secret_names)
  name        = "${local.prefix}/${each.value}"
  description = "dif-general-harness ${var.name}: ${each.value} (value set by the client)"
}

# --- database ------------------------------------------------------------------------

resource "aws_db_subnet_group" "main" {
  name       = var.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "main" {
  identifier                      = var.name
  engine                          = "postgres"
  engine_version                  = "16"
  instance_class                  = local.size.db
  multi_az                        = local.size.multi_az
  allocated_storage               = local.size.storage
  max_allocated_storage           = local.size.storage * 5
  storage_type                    = "gp3"
  storage_encrypted               = true
  db_name                         = "dif"
  username                        = "dif"
  manage_master_user_password     = true
  db_subnet_group_name            = aws_db_subnet_group.main.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  backup_retention_period         = local.size.backups
  backup_window                   = "06:00-07:00"
  maintenance_window              = "sun:07:30-sun:08:30"
  auto_minor_version_upgrade      = true
  copy_tags_to_snapshot           = true
  performance_insights_enabled    = local.size.insights
  enabled_cloudwatch_logs_exports = ["postgresql"]
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = !var.deletion_protection
  final_snapshot_identifier       = var.deletion_protection ? "${var.name}-final" : null
  publicly_accessible             = false
}

# --- service -------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "instance" {
  name              = "/dif/${var.name}"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execution_db_secret" {
  name = "db-password"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [aws_db_instance.main.master_user_secret[0].secret_arn]
    }]
  })
}

resource "aws_iam_role" "task" {
  name               = "${var.name}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "task_secrets" { # its own prefix, nothing else
  name = "instance-secrets"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [for s in aws_secretsmanager_secret.instance : s.arn]
    }]
  })
}

resource "aws_ecs_cluster" "main" {
  name = var.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_ecs_task_definition" "instance" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = local.size.cpu
  memory                   = local.size.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([{
    name         = "instance"
    image        = "${aws_ecr_repository.instance.repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ containerPort = 8080, protocol = "tcp" }]
    environment = concat([
      { name = "DIF_SECRETS_BACKEND", value = "aws-secrets-manager" },
      { name = "DIF_SECRETS_PREFIX", value = local.prefix },
      { name = "AWS_REGION", value = var.region },
      { name = "DIF_DB_HOST", value = aws_db_instance.main.address },
      { name = "DIF_DB_NAME", value = "dif" },
      { name = "DIF_DB_USER", value = "dif" },
      { name = "DIF_PUBLIC_URL", value = var.public_url },
    ], local.optional_env)
    secrets = [
      { name = "DIF_DB_PASSWORD", valueFrom = "${aws_db_instance.main.master_user_secret[0].secret_arn}:password::" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.instance.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "instance"
      }
    }
  }])
}

resource "aws_lb" "main" {
  name               = substr(var.name, 0, 32)
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
}

resource "aws_lb_target_group" "instance" {
  name        = substr("${var.name}-tg", 0, 32)
  port        = 8080
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id
  health_check {
    path    = "/readyz"
    matcher = "200"
  }
}

resource "aws_lb_listener" "https" {
  count             = local.https ? 1 : 0
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.instance.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = local.https ? "redirect" : "forward"
    target_group_arn = local.https ? null : aws_lb_target_group.instance.arn
    dynamic "redirect" {
      for_each = local.https ? [1] : []
      content {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_ecs_service" "instance" {
  name            = var.name
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.instance.arn
  desired_count   = local.size.tasks
  launch_type     = "FARGATE"
  # a rolling upgrade: new tasks start and pass health checks before old ones stop
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 60
  enable_execute_command             = false # no shell into client production
  propagate_tags                     = "SERVICE"
  network_configuration {
    subnets          = local.private ? aws_subnet.private[*].id : aws_subnet.public[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = !local.private
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.instance.arn
    container_name   = "instance"
    container_port   = 8080
  }
  deployment_circuit_breaker { # a bad release rolls back to the previous task definition
    enable   = true
    rollback = true
  }
  depends_on = [aws_lb_listener.http]
}

# --- alarms --------------------------------------------------------------------------

resource "aws_sns_topic" "alarms" {
  name = "${var.name}-alarms"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

locals {
  alarms = {
    "5xx" = {
      namespace = "AWS/ApplicationELB", metric = "HTTPCode_Target_5XX_Count", stat = "Sum",
      threshold = 10, periods = 1, compare = "GreaterThanThreshold",
      dims      = { LoadBalancer = aws_lb.main.arn_suffix, TargetGroup = aws_lb_target_group.instance.arn_suffix },
      what      = "the instance answered with 5xx errors"
    }
    "unhealthy" = {
      namespace = "AWS/ApplicationELB", metric = "UnHealthyHostCount", stat = "Maximum",
      threshold = 0, periods = 3, compare = "GreaterThanThreshold",
      dims      = { LoadBalancer = aws_lb.main.arn_suffix, TargetGroup = aws_lb_target_group.instance.arn_suffix },
      what      = "a task fails its health check (/readyz)"
    }
    "cpu" = {
      namespace = "AWS/ECS", metric = "CPUUtilization", stat = "Average",
      threshold = 85, periods = 3, compare = "GreaterThanThreshold",
      dims      = { ClusterName = aws_ecs_cluster.main.name, ServiceName = var.name },
      what      = "the service runs hot: consider the next size"
    }
    "memory" = {
      namespace = "AWS/ECS", metric = "MemoryUtilization", stat = "Average",
      threshold = 85, periods = 3, compare = "GreaterThanThreshold",
      dims      = { ClusterName = aws_ecs_cluster.main.name, ServiceName = var.name },
      what      = "the service is short of memory"
    }
    "db-storage" = {
      namespace = "AWS/RDS", metric = "FreeStorageSpace", stat = "Minimum",
      threshold = 2147483648, periods = 1, compare = "LessThanThreshold",
      dims      = { DBInstanceIdentifier = aws_db_instance.main.identifier },
      what      = "the database has less than 2 GB free"
    }
    "db-cpu" = {
      namespace = "AWS/RDS", metric = "CPUUtilization", stat = "Average",
      threshold = 85, periods = 3, compare = "GreaterThanThreshold",
      dims      = { DBInstanceIdentifier = aws_db_instance.main.identifier },
      what      = "the database runs hot"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "instance" {
  for_each            = local.alarms
  alarm_name          = "${var.name}-${each.key}"
  alarm_description   = each.value.what
  namespace           = each.value.namespace
  metric_name         = each.value.metric
  statistic           = each.value.stat
  period              = 300
  evaluation_periods  = each.value.periods
  threshold           = each.value.threshold
  comparison_operator = each.value.compare
  dimensions          = each.value.dims
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  ok_actions          = [aws_sns_topic.alarms.arn]
}
