/* 이 파일을 secrets.h 로 복사한 뒤 실제 값을 채운다.
 * secrets.h 는 .gitignore 대상이라 커밋되지 않는다.
 *
 *   cp secrets_example.h secrets.h
 *
 * 저장소가 public이므로 와이파이 비밀번호와 API 키를 .ino에 직접 쓰지 말 것. */
#pragma once

#define SECRET_WIFI_SSID     "your-ssid"
#define SECRET_WIFI_PASSWORD "your-wifi-password"

// raspberry-pi/.env 의 API_KEY 와 반드시 일치해야 한다. 틀리면 전부 401.
#define SECRET_API_KEY       "your-api-key"
