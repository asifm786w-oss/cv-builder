from models import CV, Experience, Education
from utils import render_cv_pdf_bytes

cv = CV(
    full_name="Test User",
    title="Retail & Sales Professional",
    email="test@example.com",
    phone="01234 567890",
    full_address="123 Test Street, Walsall, WS2 9UT",
    location="Walsall, UK",
    summary="Energetic retail professional with 19 years' experience...",
    skills=["Sales", "Customer Service", "Team Leadership"],
    experiences=[
        Experience(
            job_title="Shop Floor Sales Assistant",
            company="Morrisons",
            location="Walsall",
            start_date="Nov 2004",
            end_date="Jun 2023",
            description="• Delivered exceptional customer service\n• Trained and supported team members",
        )
    ],
    education=[
        Education(
            degree="BSc (Hons) Business and Marketing (Level 4–6) – In Progress",
            institution="British Academy of Jewellery – Birmingham",
            location="Birmingham",
            start_date="2023",
            end_date="Present",
        )
    ],
    references=None,
)

pdf_bytes = render_cv_pdf_bytes(cv, template_name="cv_elegant.html")

with open("test_playwright_cv.pdf", "wb") as f:
    f.write(pdf_bytes)

print("PDF written to test_playwright_cv.pdf")
