variable "aws_region" {
  description = "AWS region for EMG infrastructure"
  type        = string
  default     = "us-west-2"
}

variable "project_name" {
  description = "Project name used in resource naming"
  type        = string
  default     = "emg"
}

variable "environment" {
  description = "Deployment environment"
  type        = string
  default     = "staging"
}

variable "image_tag" {
  description = "Container image tag to deploy"
  type        = string
  default     = "bootstrap"
}

variable "container_port" {
  description = "Port exposed by the FastAPI container"
  type        = number
  default     = 8000
}

variable "task_cpu" {
  description = "Fargate task CPU units"
  type        = number
  default     = 256
}

variable "task_memory" {
  description = "Fargate task memory in MiB"
  type        = number
  default     = 512
}

variable "desired_count" {
  description = "Number of running ECS tasks"
  type        = number
  default     = 0
}

variable "vpc_cidr" {
  description = "CIDR block for the EMG VPC"
  type        = string
  default     = "10.20.0.0/16"
}

variable "allowed_origin" {
  description = "Browser origin allowed by the EMG API"
  type        = string
  default     = "https://example.invalid"
}