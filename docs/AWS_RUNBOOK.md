# AWS Operations & Deployment Runbook

## 1. Prerequisites & Environment Initialization
Ensure AWS CLI v2 is installed and authorized. Set variables for your deployment session:
```bash
export AWS_PROFILE=default
export AWS_REGION=ap-south-1
export AWS_PAGER=""
export ACCT=$(aws sts get-caller-identity --query Account --output text)
echo "Operating on AWS Account: $ACCT in Region: $AWS_REGION"
mkdir -p infra/state && touch infra/state/resources.env
```

## 2. Resource Provisioning

### 2.1 Private S3 Bucket for Releases and Backups
```bash
export BUCKET="alloc-platform-${ACCT}-${AWS_REGION}"
aws s3api create-bucket \
    --bucket "$BUCKET" \
    --region "$AWS_REGION" \
    --create-bucket-configuration LocationConstraint="$AWS_REGION"

# Block all public access
aws s3api put-public-access-block \
    --bucket "$BUCKET" \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# Enable default server-side encryption (AES256)
aws s3api put-bucket-encryption \
    --bucket "$BUCKET" \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'

echo "BUCKET=$BUCKET" >> infra/state/resources.env
```

### 2.2 IAM Instance Role & Policy (Least Privilege)
```bash
cat > infra/iam/s3policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/releases/*"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/backups/*"
    },
    {
      "Effect": "Allow",
      "Action": ["cloudwatch:GetMetricStatistics"],
      "Resource": "*"
    }
  ]
}
EOF

aws iam create-role \
    --role-name alloc-platform-ec2 \
    --assume-role-policy-document file://infra/iam/trust.json \
    --tags Key=Project,Value=alloc-platform

aws iam attach-role-policy \
    --role-name alloc-platform-ec2 \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

aws iam put-role-policy \
    --role-name alloc-platform-ec2 \
    --policy-name alloc-s3-cw \
    --policy-document file://infra/iam/s3policy.json

aws iam create-instance-profile \
    --instance-profile-name alloc-platform-ec2

aws iam add-role-to-instance-profile \
    --instance-profile-name alloc-platform-ec2 \
    --role-name alloc-platform-ec2

sleep 15
```

### 2.3 Security Group with Zero Inbound Rules
```bash
export VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
export SG_ID=$(aws ec2 create-security-group \
    --group-name alloc-platform-sg \
    --description 'alloc platform, zero inbound' \
    --vpc-id "$VPC_ID" \
    --tag-specifications 'ResourceType=security-group,Tags=[{Key=Project,Value=alloc-platform}]' \
    --query GroupId --output text)

# Verify zero inbound rules: must return []
aws ec2 describe-security-groups --group-ids "$SG_ID" --query 'SecurityGroups[0].IpPermissions'

echo "SG_ID=$SG_ID" >> infra/state/resources.env
```

### 2.4 Launch EC2 Instance with User-Data Bootstrap
```bash
export ITYPE="c7i-flex.large"
export AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 --query Parameter.Value --output text)

export IID=$(aws ec2 run-instances \
    --image-id "$AMI" \
    --instance-type "$ITYPE" \
    --iam-instance-profile Name=alloc-platform-ec2 \
    --security-group-ids "$SG_ID" \
    --associate-public-ip-address \
    --metadata-options HttpTokens=required,HttpEndpoint=enabled \
    --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=20,VolumeType=gp3,DeleteOnTermination=true,Encrypted=true}' \
    --user-data file://infra/userdata.sh \
    --tag-specifications 'ResourceType=instance,Tags=[{Key=Project,Value=alloc-platform},{Key=Name,Value=alloc-host}]' \
    --query 'Instances[0].InstanceId' --output text)

echo "IID=$IID" >> infra/state/resources.env

echo "Waiting for instance to reach running state..."
aws ec2 wait instance-running --instance-ids "$IID"
aws ec2 wait instance-status-ok --instance-ids "$IID"

# Wait for SSM Agent to establish connection
echo "Checking SSM online status..."
aws ssm describe-instance-information --filters Key=InstanceIds,Values="$IID" --query 'InstanceInformationList[0].PingStatus'
```

## 3. Application Deployment
Deploy application updates using `scripts/deploy.sh`:
```bash
bash scripts/deploy.sh
```

## 4. Accessing the Platform UI via SSM Tunnel
Because no ports are publicly exposed, establish an SSM port-forwarding session:
```bash
aws ssm start-session \
    --target "$IID" \
    --document-name AWS-StartPortForwardingSession \
    --parameters '{"portNumber":["8000"],"localPortNumber":["8000"]}'
```
Open [http://localhost:8000](http://localhost:8000) on your local browser.

### 4.1 Bootstrap Admin Password Retrieval
To log in for the first time, retrieve the bootstrap password:
```bash
aws ssm send-command \
    --instance-ids "$IID" \
    --document-name AWS-RunShellScript \
    --parameters 'commands=["sudo cat /etc/alloc/bootstrap_admin.txt"]' \
    --query Command.CommandId --output text
```
Log in as username `admin` with the retrieved password, change the password in the UI, and delete `/etc/alloc/bootstrap_admin.txt`.

## 5. Cost Control & Suspension
Between sessions, avoid compute charges by stopping the instance:
```bash
aws ec2 stop-instances --instance-ids "$IID"
```
To resume:
```bash
aws ec2 start-instances --instance-ids "$IID"
# Systemd auto-starts docker, dockerproxy, and alloc-api; orphan container sweep runs on boot.
```

## 6. Full Infrastructure Teardown
To permanently terminate all cloud resources:
```bash
source infra/state/resources.env

# Terminate EC2 instance
aws ec2 terminate-instances --instance-ids "$IID"
aws ec2 wait instance-terminated --instance-ids "$IID"

# Remove Security Group
aws ec2 delete-security-group --group-id "$SG_ID"

# Remove IAM Role & Profile
aws iam remove-role-from-instance-profile --instance-profile-name alloc-platform-ec2 --role-name alloc-platform-ec2
aws iam delete-instance-profile --instance-profile-name alloc-platform-ec2
aws iam delete-role-policy --role-name alloc-platform-ec2 --policy-name alloc-s3-cw
aws iam detach-role-policy --role-name alloc-platform-ec2 --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role --role-name alloc-platform-ec2

# Remove S3 Bucket
aws s3 rm "s3://${BUCKET}" --recursive
aws s3api delete-bucket --bucket "$BUCKET"

# Confirm zero project resources remain
aws ec2 describe-volumes --filters Name=tag:Project,Values=alloc-platform --query 'Volumes[].VolumeId'
```
