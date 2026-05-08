#!/usr/bin/env python3
"""
OCI ARM 인스턴스 생성 매크로 (춘천 리전, 무한 재시도, 시간대별 대기)
"""

import oci
import sys
import time
import os
import base64
import smtplib
import logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
import pytz
import threading

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def send_email(subject, body):
    smtp_user = os.environ.get("SMTP_USER")
    smtp_password = os.environ.get("SMTP_PASSWORD")
    smtp_to = os.environ.get("SMTP_TO")
    if not all([smtp_user, smtp_password, smtp_to]):
        logger.info("SMTP 설정이 없어 이메일을 건너뜁니다.")
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = smtp_to
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as s:
            s.starttls()
            s.login(smtp_user, smtp_password)
            s.sendmail(smtp_user, [smtp_to], msg.as_string())
        logger.info("이메일 발송 완료")
    except Exception as e:
        logger.error(f"이메일 발송 실패: {e}")

def get_wait_seconds():
    """한국 시간 기준 새벽(0~5시) 30초, 낮 300초"""
    kst = pytz.timezone('Asia/Seoul')
    hour = datetime.now(kst).hour
    return 30 if 0 <= hour < 6 else 300

def wait_for_instance_running(compute_client, instance_id, timeout=900, interval=10):
    start_time = time.time()
    last_state = None
    state_log = []
    last_log_at = 0
    while time.time() - start_time < timeout:
        instance = compute_client.get_instance(instance_id).data
        state = instance.lifecycle_state
        elapsed = int(time.time() - start_time)
        entry = f"[{elapsed}s] {state}"
        if state != last_state or elapsed - last_log_at >= 60:
            state_log.append(entry)
            last_log_at = elapsed
        logger.info(f"  {entry}")
        last_state = state
        if state == "RUNNING":
            logger.info("✅ 인스턴스 실행 중")
            return (True, state, state_log)
        elif state in ["TERMINATED", "TERMINATING"]:
            logger.error(f"❌ 인스턴스가 {state} 상태로 전환됨")
            return (False, state, state_log)
        time.sleep(interval)
    logger.warning(f"⏰ 시간 초과 (15분): 최종 상태 = {last_state}")
    return (False, last_state, state_log)

def wait_for_state(compute_client, instance_id, target_states, timeout=300, interval=10):
    """인스턴스가 지정된 상태 중 하나에 도달할 때까지 대기 (TERMINATED/TERMINATING 시 즉시 False)"""
    start_time = time.time()
    state = "UNKNOWN"
    while time.time() - start_time < timeout:
        instance = compute_client.get_instance(instance_id).data
        state = instance.lifecycle_state
        logger.info(f"  상태: {state} (목표: {target_states})")
        if state in target_states:
            return True
        if state in ["TERMINATED", "TERMINATING"]:
            logger.error(f"❌ 인스턴스가 {state} 상태로 전환됨")
            return False
        time.sleep(interval)
    logger.warning(f"⏰ 타임아웃: 목표 {target_states}, 현재 {state}")
    return False

def resize_instance(compute_client, instance_id, target_ocpus, target_memory):
    """인스턴스 정지 → 형상 변경 → 시작 → RUNNING 대기"""
    logger.info(f"🔄 인스턴스 {instance_id} 확장: {target_ocpus} OCPU / {target_memory}GB")

    logger.info("  정지 요청...")
    compute_client.instance_action(instance_id, "STOP")
    if not wait_for_state(compute_client, instance_id, ["STOPPED"], timeout=300):
        logger.error("정지 실패")
        return False

    logger.info("  형상 업데이트...")
    try:
        update_details = oci.core.models.UpdateInstanceDetails(
            shape_config=oci.core.models.UpdateInstanceShapeConfigDetails(
                ocpus=target_ocpus,
                memory_in_gbs=target_memory
            )
        )
        compute_client.update_instance(instance_id, update_details)
    except Exception as e:
        logger.error(f"형상 업데이트 실패: {e}")
        try:
            compute_client.instance_action(instance_id, "START")
            wait_for_state(compute_client, instance_id, ["RUNNING"], timeout=120)
        except Exception:
            pass
        return False

    logger.info("  시작 요청...")
    compute_client.instance_action(instance_id, "START")
    if not wait_for_state(compute_client, instance_id, ["RUNNING"], timeout=300):
        logger.error("시작 실패")
        return False

    logger.info("✅ 확장 완료")
    return True

def get_public_ip(compute_client, compartment_id, instance_id):
    try:
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
    except Exception as e:
        logger.error(f"공인 IP 조회 실패: {e}")
        return None

def main():
    # 환경 변수에서 필수 정보 읽기 (Docker 환경에 적합)
    compartment_id = os.environ.get("OCI_COMPARTMENT_ID")
    subnet_id = os.environ.get("OCI_SUBNET_ID")
    availability_domain = os.environ.get("OCI_AVAILABILITY_DOMAIN", "AP-CHUNCHEON-1-AD-1")
    image_id = os.environ.get("OCI_IMAGE_ID")
    ssh_public_key = os.environ.get("OCI_SSH_PUBLIC_KEY")
    key_path = os.environ.get("OCI_KEY_PATH", "/tmp/oci_api_key.pem")

    # OCI_KEY_CONTENT 환경 변수로 키 내용이 전달된 경우 파일로 저장
    key_content = os.environ.get("OCI_KEY_CONTENT")
    if key_content:
        with open(key_path, "w") as f:
            f.write(base64.b64decode(key_content).decode())
        os.chmod(key_path, 0o600)

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

    # 공통 인스턴스 상세 (shape 제외)
    base_details = dict(
        compartment_id=compartment_id,
        availability_domain=availability_domain,
        display_name="devforge",
        shape="VM.Standard.A1.Flex",
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=image_id, boot_volume_size_in_gbs=50),
        subnet_id=subnet_id,
        metadata={"ssh_authorized_keys": ssh_public_key}
    )

    # 직접 생성 (4 OCPU / 24GB)
    details_direct = oci.core.models.LaunchInstanceDetails(
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=4, memory_in_gbs=24),
        **base_details
    )
    # 소형 선점 (1 OCPU / 6GB → 확장)
    details_small = oci.core.models.LaunchInstanceDetails(
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=1, memory_in_gbs=6),
        **base_details
    )

    done_event = threading.Event()
    winner_lock = threading.Lock()
    winner_info = {}

    stats = {"small_ok": 0, "resize_fail": 0, "direct_attempts": 0}
    stats_lock = threading.Lock()

    def run_direct():
        """직접 4/24 생성 전략"""
        nonlocal winner_info
        client = oci.core.ComputeClient(config)
        attempt = 0
        while not done_event.is_set():
            attempt += 1
            wait_sec = get_wait_seconds()
            now_str = datetime.now(pytz.timezone('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')
            logger.info(f"[{now_str}] 🚀 [직접] 생성 시도 #{attempt} (다음 대기: {wait_sec}초)")
            with stats_lock:
                stats["direct_attempts"] += 1

            instance_id = None
            try:
                response = client.launch_instance(details_direct)
                instance_id = response.data.id
                logger.info(f"✅ [직접] 생성 성공! OCID: {instance_id}")

                logger.info("⏳ [직접] RUNNING 상태 대기 중 (최대 15분)...")
                success, final_state, state_log = wait_for_instance_running(client, instance_id)
                if success:
                    with winner_lock:
                        if not done_event.is_set():
                            done_event.set()
                            winner_info = {"strategy": "직접", "instance_id": instance_id, "client": client}
                            return
                    # 패배: 이미 다른 쪽이 성공
                    logger.info("[직접] 다른 전략이 이미 성공, 정리합니다.")
                    try:
                        client.terminate_instance(instance_id)
                    except Exception:
                        pass
                    return
                elif final_state in ["TERMINATED", "TERMINATING"]:
                    logger.warning(f"[직접] {final_state} 상태, 종료 후 재시도...")
                    try:
                        client.terminate_instance(instance_id)
                    except Exception:
                        pass
                    now_str = datetime.now(pytz.timezone('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')
                    body = (
                        f"[직접 전략] OCI ARM 인스턴스 생성 실패\n\n"
                        f"시간: {now_str} (KST)\n"
                        f"OCID: {instance_id}\n"
                        f"최종 상태: {final_state}\n\n"
                        f"상태 변화 로그:\n"
                        + "\n".join(state_log)
                    )
                    send_email("[OCI ARM] 생성 실패 - TERMINATED (직접)", body)
                    time.sleep(wait_sec)
                    continue
                else:
                    logger.warning(f"[직접] 시간 초과 (현재 상태: {final_state}), 정리 후 재시도...")
                    try:
                        client.terminate_instance(instance_id)
                    except Exception:
                        pass
                    time.sleep(wait_sec)
                    continue

            except oci.exceptions.ServiceError as e:
                if "Out of" in str(e) or e.status == 429:
                    logger.warning(f"[직접] 용량 부족 또는 제한")
                else:
                    logger.error(f"[직접] OCI 오류: {e}")
                if instance_id:
                    try:
                        client.terminate_instance(instance_id)
                    except Exception:
                        pass
                time.sleep(wait_sec)
                continue
            except Exception as e:
                logger.error(f"[직접] 기타 오류: {e}, 재시도...")
                if instance_id:
                    try:
                        client.terminate_instance(instance_id)
                    except Exception:
                        pass
                time.sleep(wait_sec)
                continue

    def run_small():
        """소형 1/6 생성 → 확장 전략"""
        nonlocal winner_info
        client = oci.core.ComputeClient(config)
        attempt = 0
        while not done_event.is_set():
            attempt += 1
            wait_sec = get_wait_seconds()
            now_str = datetime.now(pytz.timezone('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')
            logger.info(f"[{now_str}] 🚀 [소형] 생성 시도 #{attempt} (다음 대기: {wait_sec}초)")

            instance_id = None
            try:
                response = client.launch_instance(details_small)
                instance_id = response.data.id
                logger.info(f"✅ [소형] 1/6 생성 성공! OCID: {instance_id}")

                logger.info("⏳ [소형] RUNNING 상태 대기 중 (최대 15분)...")
                success, final_state, state_log = wait_for_instance_running(client, instance_id)
                if not success:
                    if final_state in ["TERMINATED", "TERMINATING"]:
                        logger.warning(f"[소형] {final_state} 상태, 종료 후 재시도...")
                        try:
                            client.terminate_instance(instance_id)
                        except Exception:
                            pass
                        now_str = datetime.now(pytz.timezone('Asia/Seoul')).strftime('%Y-%m-%d %H:%M:%S')
                        body = (
                            f"[소형 전략] OCI ARM 인스턴스 생성 실패\n\n"
                            f"시간: {now_str} (KST)\n"
                            f"OCID: {instance_id}\n"
                            f"최종 상태: {final_state}\n\n"
                            f"상태 변화 로그:\n"
                            + "\n".join(state_log)
                        )
                        send_email("[OCI ARM] 생성 실패 - TERMINATED (소형)", body)
                    else:
                        logger.warning(f"[소형] 시간 초과 (현재 상태: {final_state}), 정리 후 재시도...")
                        try:
                            client.terminate_instance(instance_id)
                        except Exception:
                            pass
                    time.sleep(wait_sec)
                    continue

                # 1/6 RUNNING → 카운트
                with stats_lock:
                    stats["small_ok"] += 1

                # 확장 시도
                if done_event.is_set():
                    logger.info("[소형] 다른 전략이 이미 성공, 정리합니다.")
                    try:
                        client.terminate_instance(instance_id)
                    except Exception:
                        pass
                    return

                # RESIZE 재시도 (최대 5회, 60초 간격)
                resize_ok = False
                for resize_attempt in range(5):
                    if done_event.is_set():
                        try:
                            client.terminate_instance(instance_id)
                        except Exception:
                            pass
                        return
                    logger.info(f"[소형] 확장 시도 {resize_attempt + 1}/5")
                    if resize_instance(client, instance_id, 4, 24):
                        resize_ok = True
                        break
                    with stats_lock:
                        stats["resize_fail"] += 1
                    if resize_attempt < 4:
                        logger.info("[소형] 확장 실패, 60초 후 재시도...")
                        if done_event.wait(60):
                            try:
                                client.terminate_instance(instance_id)
                            except Exception:
                                pass
                            return

                if resize_ok:
                    with winner_lock:
                        if not done_event.is_set():
                            done_event.set()
                            winner_info = {"strategy": "소형→확장", "instance_id": instance_id, "client": client}
                            return
                    logger.info("[소형→확장] 다른 전략이 이미 성공, 정리합니다.")
                    try:
                        client.terminate_instance(instance_id)
                    except Exception:
                        pass
                    return
                else:
                    logger.warning("[소형] 5회 확장 실패, 1/6 인스턴스 정리 후 재시도...")
                    try:
                        client.terminate_instance(instance_id)
                    except Exception:
                        pass
                    time.sleep(wait_sec)
                    continue

            except oci.exceptions.ServiceError as e:
                if "Out of" in str(e) or e.status == 429:
                    logger.warning(f"[소형] 용량 부족 또는 제한")
                else:
                    logger.error(f"[소형] OCI 오류: {e}")
                if instance_id:
                    try:
                        client.terminate_instance(instance_id)
                    except Exception:
                        pass
                time.sleep(wait_sec)
                continue
            except Exception as e:
                logger.error(f"[소형] 기타 오류: {e}, 재시도...")
                if instance_id:
                    try:
                        client.terminate_instance(instance_id)
                    except Exception:
                        pass
                time.sleep(wait_sec)
                continue

    def check_orphans():
        """종료되지 않은 인스턴스 (고아) 목록 반환"""
        client = oci.core.ComputeClient(config)
        try:
            instances = client.list_instances(compartment_id=compartment_id).data
            active_states = {"MOVING", "PROVISIONING", "RUNNING", "STARTING", "STOPPING", "STOPPED", "CREATING_IMAGE"}
            result = []
            for inst in instances:
                if inst.lifecycle_state in active_states:
                    result.append(f"  - {inst.id} [{inst.lifecycle_state}] {inst.display_name}")
            return result
        except Exception as e:
            logger.error(f"고아 확인 중 오류: {e}")
            return None

    def status_reporter():
        """오전 9시 / 저녁 9시 (KST) 기준 현황 메일 발송"""
        kst = pytz.timezone('Asia/Seoul')
        while not done_event.is_set():
            now = datetime.now(kst)
            # 다음 보고 시간 계산
            if now.hour < 9:
                next_report = now.replace(hour=9, minute=0, second=0, microsecond=0)
            elif now.hour < 21:
                next_report = now.replace(hour=21, minute=0, second=0, microsecond=0)
            else:
                next_report = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=1)
            wait_seconds = (next_report - now).total_seconds()
            logger.info(f"📊 다음 현황 보고: {next_report.strftime('%m/%d %H:%M')} KST ({wait_seconds/3600:.1f}시간 후)")
            if done_event.wait(wait_seconds):
                return
            with stats_lock:
                body = (
                    f"OCI ARM 생성 현황 ({next_report.strftime('%Y-%m-%d %H:%M')} KST 기준)\n\n"
                    f"1/6 launch 성공: {stats['small_ok']}회\n"
                    f"resize 실패: {stats['resize_fail']}회\n"
                    f"4/24 직접 시도: {stats['direct_attempts']}회\n"
                )
                stats["small_ok"] = 0
                stats["resize_fail"] = 0
                stats["direct_attempts"] = 0
            orphans = check_orphans()
            if orphans is None:
                body += "\n고아 인스턴스: 확인 실패"
            elif orphans:
                body += f"\n고아 인스턴스: {len(orphans)}개\n" + "\n".join(orphans)
            else:
                body += "\n고아 인스턴스: 0개"
            send_email("[OCI ARM] 12시간 현황", body)

    t_direct = threading.Thread(target=run_direct, daemon=True, name="direct")
    t_small = threading.Thread(target=run_small, daemon=True, name="small")
    t_status = threading.Thread(target=status_reporter, daemon=True, name="status")
    t_direct.start()
    t_small.start()
    t_status.start()

    logger.info("🚀 병렬 생성 시작: [직접 4/24] + [소형 1/6→확장]")

    # 완료 대기
    while not done_event.is_set():
        t_direct.join(timeout=1)
        t_small.join(timeout=1)
        if not t_direct.is_alive() and not t_small.is_alive():
            logger.error("양쪽 스레드 모두 종료됨")
            break

    if winner_info:
        inst_id = winner_info["instance_id"]
        strategy = winner_info["strategy"]
        client = winner_info["client"]
        public_ip = get_public_ip(client, compartment_id, inst_id)
        ssh_cmd = ""
        if public_ip:
            ssh_cmd = f"ssh -i /path/to/private_key opc@{public_ip}"
            logger.info(f"\n🔗 인스턴스 접속 정보 ({strategy}):")
            logger.info(ssh_cmd)
            body = f"OCI ARM 인스턴스 생성 성공! ({strategy})\n\nOCID: {inst_id}\nPublic IP: {public_ip}\nSSH: {ssh_cmd}"
            send_email(f"[OCI ARM] 인스턴스 생성 성공! ({strategy})", body)
        else:
            logger.warning("⚠️ 공인 IP를 찾을 수 없습니다.")
            body = f"OCI ARM 인스턴스 생성 성공! ({strategy})\n\nOCID: {inst_id}\n(공인 IP 없음)"
            send_email(f"[OCI ARM] 인스턴스 생성 성공 ({strategy}, IP 없음)", body)

    logger.info("작업 완료. 컨테이너 유지 중...")
    while True:
        time.sleep(3600)

if __name__ == "__main__":
    main()
