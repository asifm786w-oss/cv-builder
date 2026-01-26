# models.py
from typing import List, Optional
from pydantic import BaseModel

class Experience(BaseModel):
    job_title: str
    company: Optional[str] = None
    location: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    description: Optional[str] = None

class Education(BaseModel):
    degree: str
    institution: str
    location: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None

class CV(BaseModel):
    full_name: str
    title: Optional[str] = None
    email: str
    phone: Optional[str] = None
    location: Optional[str] = None
    full_address: Optional[str] = None  # ✅ NEW: optional full address
    summary: Optional[str] = None
    skills: List[str] = []
    experiences: List[Experience] = []
    education: List[Education] = []
    references: Optional[str] = None    # ✅ already fine
