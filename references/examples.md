# VNG Cloud Terraform — Complete Examples (Bridge File)

File này kết nối provider resource docs với skill, cung cấp example Terraform
đầy đủ cho từng use case thực tế. Claude đọc file này khi cần gen code.

---

## USE CASE 1: VM + Data Volume (Server with attached disk)

```hcl
# Tạo VM kèm data disk riêng — dùng khi cần storage tách biệt khỏi OS disk

resource "vngcloud_vserver_network" "network" {
  project_id = var.project_id
  name       = "net-example"
  cidr       = "10.250.0.0/16"
  zone_id    = "HCM03-1A"   # tên zone, KHÔNG dùng UUID
}

resource "vngcloud_vserver_subnet" "subnet" {
  project_id = var.project_id
  name       = "sub-example"
  cidr       = "10.250.1.0/24"
  network_id = vngcloud_vserver_network.network.id
  zone_id    = "HCM03-1A"
}

resource "vngcloud_vserver_server" "server" {
  for_each          = toset(var.server_names)
  project_id        = var.project_id
  name              = each.value
  encryption_volume = false
  attach_floating   = true
  flavor_id         = var.flavor_id         # từ references/<zone>/flavors.json
  image_id          = var.image_id          # từ references/<zone>/images.json
  network_id        = vngcloud_vserver_network.network.id
  root_disk_size    = var.root_disk_size    # GB, min 20
  root_disk_type_id = var.volume_type_id    # từ references/<zone>/volume-types.json
  security_group    = var.security_group_id_list
  subnet_id         = vngcloud_vserver_subnet.subnet.id
  action            = "start"
  user_name         = var.user_name
  user_password     = var.user_password
  zone_id           = "HCM03-1A"
  lifecycle { create_before_destroy = true }
}

# Data volume — riêng biệt với OS disk
resource "vngcloud_vserver_volume" "data_volume" {
  for_each       = toset(var.server_names)
  name           = "${each.value}-data"
  size           = var.data_disk_size       # GB
  volume_type_id = var.volume_type_id
  project_id     = var.project_id
  multi_attach   = false
  zone_id        = "HCM03-1A"
  lifecycle { create_before_destroy = true }
}

# Gắn data volume vào server
resource "vngcloud_vserver_volume_attach" "attach" {
  for_each   = toset(var.server_names)
  project_id = var.project_id
  volume_id  = vngcloud_vserver_volume.data_volume[each.key].id
  server_id  = vngcloud_vserver_server.server[each.key].id
}

output "server_ids"   { value = { for k, v in vngcloud_vserver_server.server : k => v.id } }
output "volume_ids"   { value = { for k, v in vngcloud_vserver_volume.data_volume : k => v.id } }
```

---

## USE CASE 2: Network + Subnet + Route Table

```hcl
# Tạo VPC đầy đủ với custom route table

resource "vngcloud_vserver_network" "network" {
  project_id = var.project_id
  name       = "net-prod"
  cidr       = "10.250.0.0/16"
  zone_id    = "HCM03-1A"
}

resource "vngcloud_vserver_subnet" "subnet" {
  project_id = var.project_id
  name       = "sub-prod"
  cidr       = "10.250.1.0/24"
  network_id = vngcloud_vserver_network.network.id
  zone_id    = "HCM03-1A"
}

# Route table custom — dùng khi cần định tuyến traffic đặc biệt
resource "vngcloud_vserver_route_table" "route_table" {
  project_id = var.project_id
  name       = "rt-prod"
  network_id = vngcloud_vserver_network.network.id

  route {
    destination_cidr_block = "10.0.0.0/8"
    target                 = "10.250.0.1"
  }
}

output "network_id" { value = vngcloud_vserver_network.network.id }
output "subnet_id"  { value = vngcloud_vserver_subnet.subnet.id }
```

---

## USE CASE 3: Virtual IP (VIP) — High Availability

```hcl
# VIP dùng cho HA setup — 2 server dùng chung 1 IP floating

resource "vngcloud_vserver_vip" "vip" {
  project_id  = var.project_id
  name        = "vip-ha"
  description = "Virtual IP for HA setup"
  subnet_id   = var.subnet_id   # sub-xxx
}

output "vip_address" { value = vngcloud_vserver_vip.vip.vip_address }
output "vip_id"      { value = vngcloud_vserver_vip.vip.id }
```

---

## USE CASE 4: Full Stack — VM + LB + Volume

```hcl
# Triển khai đầy đủ: Network + VM + Data Disk + Load Balancer

resource "vngcloud_vserver_network" "network" {
  project_id = var.project_id; name = "net-stack"; cidr = "10.250.0.0/16"; zone_id = var.zone_id
}
resource "vngcloud_vserver_subnet" "subnet" {
  project_id = var.project_id; name = "sub-stack"; cidr = "10.250.1.0/24"
  network_id = vngcloud_vserver_network.network.id; zone_id = var.zone_id
}

resource "vngcloud_vserver_server" "server" {
  for_each          = toset(var.server_names)
  project_id        = var.project_id
  name              = each.value
  encryption_volume = false
  attach_floating   = false   # backend servers không cần floating IP khi có LB
  flavor_id         = var.flavor_id
  image_id          = var.image_id
  network_id        = vngcloud_vserver_network.network.id
  root_disk_size    = var.root_disk_size
  root_disk_type_id = var.volume_type_id
  security_group    = var.security_group_id_list
  subnet_id         = vngcloud_vserver_subnet.subnet.id
  action            = "start"
  user_name         = var.user_name
  user_password     = var.user_password
  zone_id           = var.zone_id
  lifecycle { create_before_destroy = true }
}

resource "vngcloud_vserver_volume" "data_volume" {
  for_each       = var.data_disk_size > 0 ? toset(var.server_names) : toset([])
  name           = "${each.value}-data"
  size           = var.data_disk_size
  volume_type_id = var.volume_type_id
  project_id     = var.project_id
  zone_id        = var.zone_id
  lifecycle { create_before_destroy = true }
}
resource "vngcloud_vserver_volume_attach" "attach" {
  for_each   = var.data_disk_size > 0 ? toset(var.server_names) : toset([])
  project_id = var.project_id
  volume_id  = vngcloud_vserver_volume.data_volume[each.key].id
  server_id  = vngcloud_vserver_server.server[each.key].id
}

data "vngcloud_vlb_lb_packages" "packages" { project_id = var.project_id }

resource "vngcloud_vlb_load_balancer" "lb" {
  project_id = var.project_id
  name       = "lb-stack"
  package_id = data.vngcloud_vlb_lb_packages.packages.packages[0].uuid
  scheme     = "Internet"
  subnet_id  = vngcloud_vserver_subnet.subnet.id
  type       = "Layer 7"
  zone_id    = var.zone_id
}

resource "vngcloud_vlb_pool" "pool" {
  project_id       = var.project_id
  load_balancer_id = vngcloud_vlb_load_balancer.lb.id
  name             = "pool-stack"
  protocol         = "HTTP"
  algorithm        = "ROUND_ROBIN"
  stickiness       = false
  tls_encryption   = false
  health_monitor {
    health_check_method   = "GET"
    health_check_path     = "/"
    health_check_protocol = "HTTP"
    healthy_threshold     = 3
    unhealthy_threshold   = 3
    interval              = 30
    timeout               = 5
    success_code          = 200
    http_version          = "1.0"
  }
  dynamic "members" {
    for_each = vngcloud_vserver_server.server
    content {
      name         = members.key
      ip_address   = members.value.internal_ip   # IP nội bộ của server
      port         = 80
      monitor_port = 80
      weight       = 1
      backup       = false
    }
  }
}

resource "vngcloud_vlb_listener" "listener" {
  project_id         = var.project_id
  load_balancer_id   = vngcloud_vlb_load_balancer.lb.id
  name               = "listener-80"
  protocol           = "HTTP"
  protocol_port      = 80
  allowed_cidrs      = "0.0.0.0/0"
  default_pool_id    = vngcloud_vlb_pool.pool.id
  timeout_client     = 50
  timeout_connection = 5
  timeout_member     = 60
}

output "lb_address"  { value = vngcloud_vlb_load_balancer.lb.address }
output "server_ids"  { value = { for k, v in vngcloud_vserver_server.server : k => v.id } }
```

---

## USE CASE 5: Secgroup + Rules (standalone)

```hcl
resource "vngcloud_vserver_secgroup" "sg" {
  project_id  = var.project_id
  name        = "sg-web"
  description = "Security group for web servers"
}

# SSH
resource "vngcloud_vserver_secgrouprule" "ssh" {
  project_id        = var.project_id
  security_group_id = vngcloud_vserver_secgroup.sg.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 22
  port_range_max    = 22
  remote_ip_prefix  = "0.0.0.0/0"
}

# HTTP
resource "vngcloud_vserver_secgrouprule" "http" {
  project_id        = var.project_id
  security_group_id = vngcloud_vserver_secgroup.sg.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 80
  port_range_max    = 80
  remote_ip_prefix  = "0.0.0.0/0"
}

# HTTPS
resource "vngcloud_vserver_secgrouprule" "https" {
  project_id        = var.project_id
  security_group_id = vngcloud_vserver_secgroup.sg.id
  direction         = "ingress"
  ethertype         = "IPv4"
  protocol          = "tcp"
  port_range_min    = 443
  port_range_max    = 443
  remote_ip_prefix  = "0.0.0.0/0"
}

# Egress all
resource "vngcloud_vserver_secgrouprule" "egress" {
  project_id        = var.project_id
  security_group_id = vngcloud_vserver_secgroup.sg.id
  direction         = "egress"
  ethertype         = "IPv4"
  protocol          = ""
  remote_ip_prefix  = "0.0.0.0/0"
}

output "secgroup_id" { value = vngcloud_vserver_secgroup.sg.id }
```

---

## USE CASE 6: VKS — Private Cluster (Production)

```hcl
# VKS cluster production với CILIUM_NATIVE_ROUTING + private nodes

provider "vngcloud" {
  token_url        = "https://iamapis.vngcloud.vn/accounts-api/v2/auth/token"
  client_id        = var.client_id
  client_secret    = var.client_secret
  vserver_base_url = "https://hcm-3.api.vngcloud.vn/vserver/vserver-gateway"
  vlb_base_url     = "https://hcm-3.api.vngcloud.vn/vserver/vlb-gateway"
  vks_base_url     = "https://vks.api.vngcloud.vn"   # HCM
  # vks_base_url  = "https://vks-han-1.api.vngcloud.vn"  # HAN
}

resource "vngcloud_vks_cluster" "cluster" {
  name                           = var.cluster_name
  description                    = "Production cluster"
  version                        = "v1.29.13-vks.1740045600"
  cidr                           = "172.16.0.0/16"
  vpc_id                         = var.network_id      # net-xxx — BẮT BUỘC
  subnet_id                      = var.subnet_id       # sub-xxx — BẮT BUỘC
  az_strategy                    = "SINGLE"
  network_type                   = "CILIUM_NATIVE_ROUTING"
  enable_private_cluster         = true
  node_netmask_size              = 25
  secondary_subnets              = ["10.111.128.0/20"]
  enable_service_endpoint        = true
  enabled_load_balancer_plugin   = true
  enabled_block_store_csi_plugin = true
  lifecycle { create_before_destroy = true }
}

resource "vngcloud_vks_cluster_node_group" "nodes" {
  cluster_id           = vngcloud_vks_cluster.cluster.id
  name                 = var.node_group_name
  num_nodes            = var.node_count
  flavor_id            = var.node_flavor_id   # từ references/<zone>/flavors.json
  image_id             = var.node_image_id    # lấy từ VKS Portal > System Image
  subnet_id            = var.subnet_id
  disk_size            = var.node_disk_size
  disk_type            = var.volume_type_id   # từ references/<zone>/volume-types.json
  ssh_key_id           = var.ssh_key_id       # ssh-xxx — BẮT BUỘC
  enable_private_nodes = true
  secondary_subnets    = ["10.111.160.0/20"]
  auto_scale_config {
    min_size = var.node_min
    max_size = var.node_max
  }
  upgrade_config {
    strategy        = "SURGE"
    max_surge       = 1
    max_unavailable = 0
  }
  lifecycle { create_before_destroy = true }
}

output "cluster_id"     { value = vngcloud_vks_cluster.cluster.id }
output "cluster_config" { value = vngcloud_vks_cluster.cluster.config; sensitive = true }
```

---

## USE CASE 7: vDB Backup Storage

```hcl
data "vngcloud_vdb_backup_storage_package" "pkg_200gb" {
  name = "db.backup.quota.200GB"
}

resource "vngcloud_vdb_relational_backup_storage" "backup_storage" {
  backup_storage_package_id = data.vngcloud_vdb_backup_storage_package.pkg_200gb.id
}

resource "vngcloud_vdb_relational_backup" "backup" {
  name        = "backup-manual"
  instance_id = vngcloud_vdb_relational_database.db.id
  backup_type = "FULL"
}
```

