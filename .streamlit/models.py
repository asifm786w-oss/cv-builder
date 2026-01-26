from typing import List, Optional
from pydantic import BaseModel, Field

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

    # house / street etc.
    full_address: Optional[str] = None

    # town / city / region
    location: Optional[str] = None

    summary: Optional[str] = None

    # use Field(default_factory=...) instead of [] to avoid shared mutable defaults
    skills: List[str] = Field(default_factory=list)
    experiences: List[Experience] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)

    references: Optional[str] = None
