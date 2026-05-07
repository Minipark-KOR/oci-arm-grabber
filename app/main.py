#!/usr/bin/env python3
"""
OCI ARM 인스턴스 생성 매크로 (춘천 리전, 무한 재시도, 시간대별 대기)
"""

import oci
import sys
import time
import os
import logging
from datetime import datetime
import pytz

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def get_wait_seconds():
    """한국 시간 기준 새벽(0~5시) 30초, 낮 300초"""
    kst = pytz.timezone('Asia/Seoul')
    hour = datetime.now(kst).hour
    return 30 if 0 <= hour < 6 else 300

def wait_for_instance_running(compute_client, instance_id, timeout=300, interval=10):
    start_time = time.time()
    while time.time() - start_time < timeout:
        instance = compute_client.get_instance(instance_id).data
        state = instance.lifecycle_state
        logger.info(f"  상태: {state}")
        if state == "RUNNING":
            logger.info("✅ 인스턴스 실행 중")
            return True
        elif state in ["TERMINATED", "TERMINATING"]:
            logger.error("❌ 인스턴스 종료됨")
            return False
        time.sleep(interval)
    logger.warning("⏰ 시간 초과: RUNNING 상태 미도달")
    return False

def get_public_ip(compute_client, compartment_id, instance_id):
    vnic_attachments = compute_client.list_vnic_attachments(
        compartment_id=compartment_id,
        instance_id=instance_id
    ).data
    if not vnic_attachments:
        return None
    vnic_id = vnic_attachments[0].vnic_id
    network_client = oci.core.VirtualNetworkClient(compute_client.config)
    vnic = network_client.get_vnic(vnic_id).data
    return vnic.public_ip

def main():
    # 환경 변수에서 필수 정보 읽기 (Docker 환경에 적합)
    compartment_id = os.environ.get("OCI_COMPARTMENT_ID")
    subnet_id = os.environ.get("OCI_SUBNET_ID")
    availability_domain = os.environ.get("OCI_AVAILABILITY_DOMAIN", "AP-CHUNCHEON-1-AD-1")
    image_id = os.environ.get("OCI_IMAGE_ID")
    ssh_public_key = os.environ.get("OCI_SSH_PUBLIC_KEY")
    key_path = os.environ.get("OCI_KEY_PATH", "/root/.oci/oci_api_key.pem")

    if not all([compartment_id, subnet_id, image_id, ssh_public_key]):
        logger.error("필수 환경 변수가 누락되었습니다.")
        sys.exit(1)

    # OCI 설정 (API Key 파일은 볼륨 마운트로 제공)
    config = {
        "user": os.environ.get("OCI_USER_OCID"),
        "tenancy": os.environ.get("OCI_TENANCY_OCID"),
        "region": "ap-chuncheon-1",
        "key_file": key_path,
        "fingerprint": os.environ.get("OCI_FINGERPRINT")
    }
    compute_client = oci.core.ComputeClient(config)

    instance_details = oci.core.models.LaunchInstanceDetails(
        compartment_id=compartment_id,
        availability_domain=availability_domain,
        display_name="chuncheon-arm-instance",
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=6),
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image_id, boot_volume_size_in_gbs=50),
        subnet_id=subnet_id,
        metadata={"ssh_authorized_keys": ssh_public_key}
    )

    attempt = 0
    while True:
        attempt += 1
        wait_sec = get_wait_seconds()
        now_str = datetime.now(pytz.timezone('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')
        logger.info(f"[{now_str}] 🚀 생성 시도 #{attempt} (다음 대기: {wait_sec}초)")

        try:
            response = compute_client.launch_instance(instance_details)
            instance_id = response.data.id
            logger.info(f"✅ 생성 성공! OCID: {instance_id}")
            logger.info(f"현재 상태: {response.data.lifecycle_state}")

            logger.info("⏳ RUNNING 상태 대기 중 (최대 5분)...")
            if wait_for_instance_running(compute_client, instance_id):
                public_ip = get_public_ip(compute_client, compartment_id, instance_id)
                if public_ip:
                    logger.info(f"\n🔗 인스턴스 접속 정보:")
                    logger.info(f"ssh -i /path/to/private_key opc@{public_ip}")
                else:
                    logger.warning("⚠️ 공인 IP를 찾을 수 없습니다.")
            else:
                logger.warning("⚠️ RUNNING 상태 도달 실패, 인스턴스를 종료합니다.")
                compute_client.terminate_instance(instance_id)
            break

        except oci.exceptions.ServiceError as e:
            if "Out of capacity" in str(e) or e.status == 429:
                logger.warning(f"⚠️ 용량 부족 또는 제한: {e}")
                time.sleep(wait_sec)
                continue
            else:
                logger.error(f"❌ 치명적 OCI 오류: {e}")
                sys.exit(1)
        except Exception as e:
            logger.error(f"❌ 기타 오류: {e}, 재시도...")
            time.sleep(wait_sec)
            continue

if __name__ == "__main__":
    main()
