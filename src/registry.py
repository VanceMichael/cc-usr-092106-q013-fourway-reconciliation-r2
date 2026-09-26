"""材料提交登记处：同一 material_id 的更正只能以更大 version 追加。

- submission_id 全局唯一，已提交内容不可修改、不可删除；
- 同 (material_type, material_id) 的版本号必须严格递增；
- 匹配引擎默认读取最新版本，历史版本仍可被人工决定的证据快照引用。
"""

from collections import defaultdict
from dataclasses import dataclass

from .contracts import ContractError, validate_envelope


class RegistryError(ValueError):
    """违反只追加登记规则。"""


@dataclass(frozen=True)
class MaterialRef:
    """指向某一具体材料版本。"""

    material_type: str
    material_id: str
    version: int
    submission_id: str

    def as_dict(self) -> dict:
        return {
            "material_type": self.material_type,
            "material_id": self.material_id,
            "version": self.version,
            "submission_id": self.submission_id,
        }


class SubmissionRegistry:
    def __init__(self) -> None:
        self._submissions: dict[str, dict] = {}
        self._versions: dict[tuple[str, str], dict[int, str]] = defaultdict(dict)

    def submit(self, envelope: dict) -> MaterialRef:
        validate_envelope(envelope)
        submission_id = envelope["submission_id"]
        if submission_id in self._submissions:
            raise RegistryError(f"提交 {submission_id} 已存在，禁止覆盖")

        key = (envelope["material_type"], envelope["material_id"])
        version = envelope["version"]
        existing = self._versions[key]
        if version in existing:
            raise RegistryError(
                f"{key} 版本 {version} 已存在，材料更正必须追加新版本"
            )
        if existing and version <= max(existing):
            raise RegistryError(
                f"{key} 新版本号必须大于 {max(existing)}"
            )
        supersedes = envelope.get("supersedes")
        if supersedes is not None and supersedes not in self._submissions:
            raise RegistryError(f"supersedes 指向的提交 {supersedes} 不存在")

        self._submissions[submission_id] = envelope
        existing[version] = submission_id
        return MaterialRef(key[0], key[1], version, submission_id)

    def get(self, submission_id: str) -> dict:
        return self._submissions[submission_id]

    def versions(self, material_type: str, material_id: str) -> list[dict]:
        ids = self._versions.get((material_type, material_id), {})
        return [self._submissions[ids[v]] for v in sorted(ids)]

    def latest(self, material_type: str, material_id: str) -> dict | None:
        versions = self.versions(material_type, material_id)
        return versions[-1] if versions else None

    def latest_all(self, material_type: str) -> list[dict]:
        result = []
        for key in self._versions:
            if key[0] == material_type:
                result.append(self.latest(*key))
        return result

    def ref_of(self, envelope: dict) -> MaterialRef:
        return MaterialRef(
            envelope["material_type"],
            envelope["material_id"],
            envelope["version"],
            envelope["submission_id"],
        )

    def has_newer_versions(self, ref: MaterialRef) -> bool:
        """决定作出后，所引材料是否又出现了新版本。"""
        versions = self._versions.get((ref.material_type, ref.material_id), {})
        return bool(versions) and max(versions) > ref.version
